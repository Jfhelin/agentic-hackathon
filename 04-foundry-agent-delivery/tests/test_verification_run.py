import unittest

from scripts.verification_run import WORKFLOW_PATH, validate_run


class VerificationRunTests(unittest.TestCase):
    def setUp(self):
        self.repository = "workshop/participant"
        self.commit = "abc123"
        self.workflow = {"id": 9, "path": WORKFLOW_PATH}
        self.run = {
            "id": 42,
            "run_attempt": 2,
            "repository": {"full_name": self.repository},
            "head_repository": {"full_name": self.repository},
            "head_sha": self.commit,
            "head_branch": "main",
            "workflow_id": 9,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
        }
        self.artifacts = [{
            "name": "validation-42-2",
            "expired": False,
            "workflow_run": {"id": 42},
        }]

    def test_exact_successful_current_run_is_accepted(self):
        self.assertEqual(
            validate_run(
                self.run, self.workflow, self.repository, self.commit,
                "main", self.commit, self.artifacts,
            ),
            "validation-42-2",
        )

    def test_wrong_repo_workflow_event_commit_attempt_or_artifact_is_rejected(self):
        mutations = [
            ("repository", lambda run: run["repository"].update(full_name="other/repo")),
            ("head repository", lambda run: run["head_repository"].update(full_name="other/repo")),
            ("workflow", lambda run: run.update(workflow_id=10)),
            ("event", lambda run: run.update(event="push")),
            ("commit", lambda run: run.update(head_sha="old")),
            ("stale default branch", None),
            ("failed", lambda run: run.update(conclusion="failure")),
            ("in progress", lambda run: run.update(status="in_progress")),
            ("wrong attempt", lambda run: run.update(run_attempt=1)),
        ]
        import copy
        for label, mutate in mutations:
            run = copy.deepcopy(self.run)
            latest = self.commit
            if mutate is None:
                latest = "new-default-commit"
            else:
                mutate(run)
            with self.subTest(label=label), self.assertRaises(ValueError):
                validate_run(
                    run, self.workflow, self.repository, self.commit,
                    "main", latest, self.artifacts,
                )
        with self.assertRaisesRegex(ValueError, "artifact"):
            validate_run(
                self.run, self.workflow, self.repository, self.commit,
                "main", self.commit, [],
            )


if __name__ == "__main__":
    unittest.main()
