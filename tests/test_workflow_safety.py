import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FETCH_WORKFLOW = ROOT / ".github" / "workflows" / "fetch.yml"
RELEASE_LOG = ROOT / "data" / "release_log.json"


class WorkflowSafetyTests(unittest.TestCase):
    def test_queued_run_fast_forwards_before_reading_state(self):
        workflow = FETCH_WORKFLOW.read_text(encoding="utf-8")

        sync_position = workflow.index("git merge --ff-only origin/main")
        fetch_position = workflow.index("run: python ci_run.py")
        self.assertLess(sync_position, fetch_position)

    def test_push_conflict_blocks_pages_deployment(self):
        workflow = FETCH_WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("continue-on-error: true", workflow)
        self.assertNotIn("git push --force-with-lease", workflow)
        self.assertNotIn("Push failed (non-critical)", workflow)

    def test_repository_dispatch_finishes_one_poll_per_external_trigger(self):
        workflow = FETCH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('POLL_ITERATIONS: "1"', workflow)

    def test_contents_api_files_are_not_staged_again(self):
        workflow = FETCH_WORKFLOW.read_text(encoding="utf-8")
        files_block = workflow.split("files=(", 1)[1].split(")", 1)[0]

        self.assertNotIn("data/run.log", files_block)
        self.assertNotIn("state.json", files_block)
        self.assertIn("Contents API", workflow)

    def test_contents_api_files_are_clean_before_rebase(self):
        workflow = FETCH_WORKFLOW.read_text(encoding="utf-8")

        restore_position = workflow.index(
            "git restore --worktree -- data/run.log state.json"
        )
        rebase_position = workflow.index("git pull --rebase origin main")
        self.assertLess(restore_position, rebase_position)

    def test_release_log_is_validated_before_commit_and_deploy(self):
        workflow = FETCH_WORKFLOW.read_text(encoding="utf-8")

        validation = "python -m json.tool data/release_log.json"
        self.assertGreaterEqual(workflow.count(validation), 2)

    def test_committed_release_log_has_no_conflict_markers(self):
        content = RELEASE_LOG.read_text(encoding="utf-8")

        self.assertNotIn("<<<<<<<", content)
        self.assertNotIn("=======", content)
        self.assertNotIn(">>>>>>>", content)
        self.assertIsInstance(json.loads(content).get("events"), list)


if __name__ == "__main__":
    unittest.main()
