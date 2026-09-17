# On-demand simulation security

`run-sim.yml` accepts `workflow_dispatch` inputs `scenarioPilotRunId` and
`projectId`, both UUIDs. The caller uses fixed ref `main` and a repository-scoped
fine-grained token with Actions write, not Contents or Workflows write.

Before adding secrets, configure the `mirofish` environment to allow only the
branch `main` (no tags). Protect main with pull requests, enforce admins, and
disable force pushes and deletions. Keep all six worker secrets in this
environment, never repository-wide: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
MIROFISH_WORKER_TOKEN, MIROFISH_CALLBACK_TOKEN, LLM_API_KEY, ZEP_API_KEY.
SUPABASE_URL is configuration stored alongside the keys.

Set LLM_BASE_URL and LLM_MODEL_NAME explicitly to an approved, verified free
provider/model. There is no paid-provider fallback. Start with
OASIS_DEFAULT_MAX_ROUNDS=1 for the live acceptance test.

The image digest is from successful build 32573774120. Workflow-only changes
do not require a new image. Review and update the digest after backend changes.
Only the host runner receives the Supabase service-role key, not the container.
Seed/callback payload files have restrictive permissions and are removed during
cleanup. Raw container logs are not published, as they may contain project data.

The simulation has an 18-minute polling budget within the 30-minute job. The
remaining twelve minutes are reserved for worker startup, readiness, the seed and
dispatch steps, and - on the failure path - the orphan update and cleanup, which
have their own step timeouts so a stuck step cannot eat the job's last minutes.

When the job fails before any terminal status, the orphan update retries up to
five times, backing off 2s, 4s, then 8s, on transport, 408, 429 and 5xx failures,
and gives up at once on any other 4xx, which retrying cannot fix. That update is
conditional on the row still being queued, dispatching or running, so no retry can
overwrite a callback that landed meanwhile. Abrupt
runner loss/cancellation and concurrent callback idempotency still need separate
recovery work; do not claim reliable long-running or interactive sessions from
this batch hosting design.

Checks: `python -m unittest discover -s tests`, `pytest backend/tests/test_sp.py`,
and actionlint on `.github/workflows/run-sim.yml`. Passing static checks is not
an end-to-end simulation test.
