"""Security contract checks; run with python -m unittest discover -s tests."""
import re
import unittest
from pathlib import Path


class RunWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (Path(__file__).parents[1] / ".github/workflows/run-sim.yml").read_text(encoding="utf-8")

    def test_dispatch_and_environment_are_main_only(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("repository_dispatch:", self.workflow)
        self.assertIn("github.ref == 'refs/heads/main'", self.workflow)
        self.assertIn("environment: mirofish", self.workflow)
        self.assertIn("RUN_ID: ${{ inputs.scenarioPilotRunId }}", self.workflow)
        self.assertIn("PROJECT_ID: ${{ inputs.projectId }}", self.workflow)

    def test_container_is_digest_pinned_and_has_no_database_key(self):
        self.assertRegex(self.workflow, r"WORKER_IMAGE: ghcr.io/tomi200208/scenario-worker@sha256:[a-f0-9]{64}\b")
        self.assertNotIn(":latest", self.workflow)
        self.assertIn('docker pull "$WORKER_IMAGE"', self.workflow)
        docker_run = self.workflow.split("docker run -d", 1)[1].split("- name:", 1)[0]
        self.assertIn('"$WORKER_IMAGE"', docker_run)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", docker_run)
        self.assertNotIn("docker logs", self.workflow)

    def test_orphaned_run_update_retries_a_bounded_number_of_times(self):
        orphan = self.workflow.split("Mark an orphaned run as failed", 1)[1].split("- name:", 1)[0]
        self.assertRegex(orphan, r"\n\s+delay=2\n")
        loop = orphan.split("for attempt in 1 2 3 4 5; do", 1)[1].split("\n          done", 1)[0]
        self.assertIn("-X PATCH", loop)
        self.assertIn("status=in.(queued,dispatching,running)", loop)
        self.assertIn("408|429|5*|000)", loop)
        self.assertIn("delay=$((delay * 2))", loop)

    def test_network_waits_are_bounded_and_no_paid_fallback_exists(self):
        for curl in re.findall(r"curl[^\n]+", self.workflow):
            self.assertIn("--max-time", curl)
        self.assertIn("SECONDS + 1200", self.workflow)
        self.assertIn("steps.validate.outcome == 'success'", self.workflow)
        self.assertNotIn("gpt-4o-mini", self.workflow)
        self.assertNotIn("api.openai.com", self.workflow)


if __name__ == "__main__":
    unittest.main()
