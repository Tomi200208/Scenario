import importlib.util
from pathlib import Path

import requests


def load_sp_module():
    module_path = Path(__file__).parents[1] / "app" / "api" / "sp.py"
    spec = importlib.util.spec_from_file_location("scenariopilot_sp_adapter", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_simulation_matches_the_mirofish_api_contract(monkeypatch):
    sp = load_sp_module()
    post_calls = []
    callback_calls = []

    def fake_post(path, **kwargs):
        post_calls.append((path, kwargs))
        if path == "/api/graph/ontology/generate":
            assert kwargs["data"]["simulation_requirement"]
            return {"data": {"project_id": "mf-project"}}
        if path == "/api/graph/build":
            return {"data": {"task_id": "build-task"}}
        if path == "/api/simulation/create":
            assert kwargs["json"]["graph_id"] == "graph-1"
            return {"data": {"simulation_id": "simulation-1"}}
        if path == "/api/simulation/prepare":
            return {"data": {"task_id": "prepare-task"}}
        if path == "/api/simulation/prepare/status":
            return {"data": {"status": "completed"}}
        if path == "/api/simulation/start":
            return {"data": {"runner_status": "running"}}
        if path == "/api/report/generate":
            return {"data": {"report_id": "report-1", "task_id": "report-task"}}
        if path == "/api/report/generate/status":
            assert kwargs["json"] == {"task_id": "report-task", "simulation_id": "simulation-1"}
            return {"data": {"status": "completed", "report_id": "report-1"}}
        raise AssertionError(f"Unexpected POST {path}")

    def fake_get(path):
        if path == "/api/graph/task/build-task":
            return {"data": {"status": "completed", "result": {"graph_id": "graph-1"}}}
        if path == "/api/simulation/simulation-1/run-status":
            return {"data": {"runner_status": "completed"}}
        if path == "/api/report/report-1":
            return {"data": {"markdown_content": "# Simulation report"}}
        raise AssertionError(f"Unexpected GET {path}")

    def immediate_poll(getter, done, timeout=None):
        payload = getter()
        assert done(payload)
        return payload

    def fake_callback(*args, **kwargs):
        callback_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(sp, "_post", fake_post)
    monkeypatch.setattr(sp, "_get", fake_get)
    monkeypatch.setattr(sp, "_poll", immediate_poll)
    monkeypatch.setattr(sp, "_callback", fake_callback)

    sp._run_simulation(
        "scenario-run",
        "scenario-project",
        {"theme": "Resilient cities", "signals": [{"title": "Heat risk"}]},
        {"url": "https://callback.example", "token": "callback-token"},
    )

    assert [path for path, _ in post_calls] == [
        "/api/graph/ontology/generate",
        "/api/graph/build",
        "/api/simulation/create",
        "/api/simulation/prepare",
        "/api/simulation/prepare/status",
        "/api/simulation/start",
        "/api/report/generate",
        "/api/report/generate/status",
    ]
    args, kwargs = callback_calls[0]
    assert args[0] == {"url": "https://callback.example", "token": "callback-token"}
    assert args[1:3] == ("scenario-run", "completed")
    assert kwargs["worker_run_id"] == "simulation-1"
    assert kwargs["report_markdown"] == "# Simulation report"


def test_callback_retries_and_sends_terminal_metadata(monkeypatch):
    sp = load_sp_module()
    calls = []

    class SuccessResponse:
        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            raise requests.ConnectionError("temporary failure")
        return SuccessResponse()

    monkeypatch.setattr(sp.requests, "post", fake_post)
    monkeypatch.setattr(sp.time, "sleep", lambda _seconds: None)

    success = sp._callback(
        {"url": "https://callback.example", "token": "callback-token"},
        "scenario-run",
        "failed",
        result={"simulationId": "simulation-1"},
        error="simulation crashed",
        worker_run_id="simulation-1",
    )

    assert success is True
    assert len(calls) == 2
    body = calls[-1][1]["json"]
    assert body["status"] == "failed"
    assert body["progress"] == 100
    assert body["error"] == "simulation crashed"
    assert body["workerRunId"] == "simulation-1"
    assert body["result"]["error"] == "simulation crashed"
