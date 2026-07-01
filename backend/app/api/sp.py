"""
ScenarioPilot integration adapter.

Exposes POST /sp/runs — the exact endpoint scenariopilot's `mirofish-run` edge function dispatches to.
Auth: `Authorization: Bearer <MIROFISH_WORKER_TOKEN>`.

Flow: accept the ScenarioPilot request, immediately return {run_id}, then in a background thread drive
MiroFish's own API (ontology → build → create → prepare → start → report) and finally POST the report
back to the ScenarioPilot callback (`mirofish-notify`) with the callback token.

The SP contract (auth, payload, background dispatch, callback shape, failure handling) is production-
ready. The MiroFish-orchestration section is marked `# VERIFY:` where the exact internal payload /
status shape should be confirmed against YOUR running MiroFish (endpoints read from the fork, but the
async task/status fields vary by version). On ANY orchestration error it calls back status="failed"
so ScenarioPilot degrades cleanly instead of hanging.
"""
import io
import os
import time
import logging
import threading

import requests
from flask import Blueprint, request, jsonify

sp_bp = Blueprint('sp', __name__)
logger = logging.getLogger('mirofish.sp')

WORKER_TOKEN = os.environ.get('MIROFISH_WORKER_TOKEN', '')
# The adapter and MiroFish run in the same container; call MiroFish's API on localhost. Defaults to
# the port MiroFish binds (Railway sets $PORT), so internal calls hit the same backend process.
INTERNAL_BASE = os.environ.get('MIROFISH_INTERNAL_BASE', f"http://127.0.0.1:{os.environ.get('PORT', '5001')}").rstrip('/')
MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
STEP_TIMEOUT_S = int(os.environ.get('SP_STEP_TIMEOUT_S', '3600'))   # per async step cap
POLL_INTERVAL_S = int(os.environ.get('SP_POLL_INTERVAL_S', '5'))


def _auth_ok(req) -> bool:
    tok = (req.headers.get('Authorization') or '').replace('Bearer', '', 1).strip()
    return bool(WORKER_TOKEN) and tok == WORKER_TOKEN


def _seed_to_text(seed: dict) -> str:
    """Flatten the ScenarioPilot seed bundle into a briefing document MiroFish can ingest."""
    if not isinstance(seed, dict):
        return str(seed)
    parts = []
    theme = seed.get('theme') or seed.get('topic')
    if theme:
        parts.append(f"THEME: {theme}")
    if seed.get('text'):
        parts.append(str(seed['text']))
    for key in ('signals', 'drivers', 'strategies', 'indicators'):
        items = seed.get(key)
        if isinstance(items, list) and items:
            names = [str(i.get('title') or i.get('name') or i) for i in items][:20]
            parts.append(f"{key.upper()}: " + '; '.join(names))
    sc = seed.get('scenario') or {}
    if isinstance(sc, dict) and sc.get('quadrants'):
        for k, q in sc['quadrants'].items():
            if isinstance(q, dict):
                parts.append(f"SCENARIO {k}: {q.get('title', '')} — {(q.get('narrative') or '')[:400]}")
    return '\n\n'.join(parts) or 'ScenarioPilot foresight seed.'


def _callback(cb: dict, run_id: str, status: str, result=None, error: str | None = None):
    body = {'token': cb.get('token'), 'runId': run_id, 'status': status, 'result': result or {}}
    if error:
        body['result'] = {'error': error}
    try:
        requests.post(cb.get('url'), json=body,
                      headers={'Authorization': f"Bearer {cb.get('token')}", 'Content-Type': 'application/json'},
                      timeout=60)
    except Exception as e:  # noqa: BLE001
        logger.error("ScenarioPilot callback failed: %s", e)


def _post(path, **kwargs):
    r = requests.post(f"{INTERNAL_BASE}{path}", timeout=180, **kwargs)
    r.raise_for_status()
    return r.json()


def _get(path):
    r = requests.get(f"{INTERNAL_BASE}{path}", timeout=180)
    r.raise_for_status()
    return r.json()


def _poll(getter, done, timeout=STEP_TIMEOUT_S):
    """Poll `getter()` until `done(payload)` is truthy or timeout. Returns the last payload."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = getter() or {}
            if done(last):
                return last
            data = last.get('data', last)
            st = str(data.get('status') or data.get('runner_status') or '').lower()
            if st in ('failed', 'error'):
                raise RuntimeError(f"MiroFish step failed: {data}")
        except requests.RequestException as e:
            logger.warning("poll transient error: %s", e)
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError("MiroFish step timed out")


def _run_simulation(sp_run_id: str, project_id: str, seed: dict, cb: dict):
    try:
        seed_text = _seed_to_text(seed)
        theme = (seed or {}).get('theme') or 'ScenarioPilot Foresight'

        # 1) Ontology + project from the seed (multipart file upload). VERIFY: field names.
        files = {'files': ('scenariopilot_seed.txt', io.BytesIO(seed_text.encode('utf-8')), 'text/plain')}
        data = {'project_name': f"SP: {theme}"[:80], 'additional_context': 'ScenarioPilot foresight seed bundle.'}
        onto = _post('/api/graph/ontology/generate', files=files, data=data)
        mf_project = (onto.get('data') or onto).get('project_id')
        if not mf_project:
            raise RuntimeError(f"no project_id from ontology/generate: {onto}")

        # 2) Build the graph, then poll the build task. VERIFY: task/status shape.
        build = _post('/api/graph/build', json={'project_id': mf_project})
        task_id = (build.get('data') or build).get('task_id')
        if task_id:
            _poll(lambda: _get(f"/api/graph/task/{task_id}"),
                  lambda p: str((p.get('data') or p).get('status', '')).lower() in ('completed', 'success', 'done'))
        graph_id = (build.get('data') or build).get('graph_id')

        # 3) Create the simulation.
        create = _post('/api/simulation/create', json={'project_id': mf_project, 'graph_id': graph_id,
                                                       'enable_twitter': True, 'enable_reddit': True})
        simulation_id = (create.get('data') or create).get('simulation_id') or (create.get('data') or create).get('id')
        if not simulation_id:
            raise RuntimeError(f"no simulation_id from create: {create}")

        # 4) Prepare entities (LLM profiles), poll prepare/status. VERIFY: status endpoint takes task_id.
        prep = _post('/api/simulation/prepare', json={'simulation_id': simulation_id})
        prep_task = (prep.get('data') or prep).get('task_id')
        if prep_task:
            _poll(lambda: _post('/api/simulation/prepare/status', json={'task_id': prep_task}),
                  lambda p: str((p.get('data') or p).get('status', '')).lower() in ('completed', 'success', 'done'))

        # 5) Start the multi-round simulation, poll run-status until finished.
        _post('/api/simulation/start', json={'simulation_id': simulation_id, 'platform': 'parallel', 'max_rounds': MAX_ROUNDS})
        _poll(lambda: _get(f"/api/simulation/{simulation_id}/run-status"),
              lambda p: str((p.get('data') or p).get('runner_status', '')).lower() in ('completed', 'finished', 'stopped', 'done'))

        # 6) Generate the report, poll, then fetch its content. VERIFY: report result shape.
        rep = _post('/api/report/generate', json={'simulation_id': simulation_id})
        report_id = (rep.get('data') or rep).get('report_id')
        report = {}
        if report_id:
            _poll(lambda: _get(f"/api/report/by-simulation/{simulation_id}"),
                  lambda p: str((p.get('data') or p).get('status', '')).lower() in ('completed', 'success', 'done'))
            report = _get(f"/api/report/{report_id}").get('data', {})

        _callback(cb, sp_run_id, 'completed', result={
            'mirofishProjectId': mf_project,
            'simulationId': simulation_id,
            'reportId': report_id,
            'report': report,
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("ScenarioPilot run failed")
        _callback(cb, sp_run_id, 'failed', error=str(e))


@sp_bp.route('/runs', methods=['POST'])
def sp_runs():
    if not _auth_ok(request):
        return jsonify({'error': 'Invalid worker token'}), 401
    data = request.get_json(silent=True) or {}
    sp_run_id = data.get('scenarioPilotRunId')
    project_id = data.get('projectId')
    seed = data.get('seedBundle') or {}
    cb = data.get('callback') or {}
    if not sp_run_id or not cb.get('url') or not cb.get('token'):
        return jsonify({'error': 'scenarioPilotRunId and callback{url,token} are required'}), 400
    threading.Thread(target=_run_simulation, args=(sp_run_id, project_id, seed, cb), daemon=True).start()
    return jsonify({'run_id': sp_run_id, 'status': 'accepted'}), 202


@sp_bp.route('/health', methods=['GET'])
def sp_health():
    return jsonify({'ok': True, 'service': 'scenariopilot-adapter', 'token_configured': bool(WORKER_TOKEN)})
