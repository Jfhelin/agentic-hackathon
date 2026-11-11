import os
from pathlib import Path
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def step_script(filename, name):
    text = (WORKFLOWS / filename).read_text()
    step = text.split(f"      - name: {name}\n", 1)[1]
    script = step.split("        run: |\n", 1)[1].split("\n      - ", 1)[0]
    return textwrap.dedent(script)


def run_guard(script, **values):
    env = os.environ.copy()
    env.update(values)
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


class DeliveryWorkflowTests(unittest.TestCase):
    def test_capture_requires_exact_branch_and_uses_same_base(self):
        filename = "weather-delivery-cloud.yml"
        script = step_script(filename, "Confirm the exact delivery branch")
        for ref, branch, expected in (
            ("refs/heads/FY27", "FY27", 0),
            ("refs/heads/rehearsal/test", "rehearsal/test", 0),
            ("refs/heads/REHEARSAL/test", "rehearsal/test", 1),
            ("refs/heads/other", "rehearsal/test", 1),
        ):
            self.assertEqual(run_guard(script, GITHUB_REF=ref, DELIVERY_BRANCH=branch).returncode, expected)
        text = (WORKFLOWS / filename).read_text()
        self.assertIn("ref: ${{ github.sha }}", text)
        self.assertIn('--base "$DELIVERY_BRANCH"', text)
        self.assertIn("vars.WEATHER_DELIVERY_BRANCH || github.event.repository.default_branch", text)

    def test_deploy_requires_exact_ref_full_commit_and_intent(self):
        filename = "weather-delivery-deploy.yml"
        script = step_script(filename, "Confirm the deployment decision and exact commit")
        good = {
            "GITHUB_REF": "refs/heads/rehearsal/test",
            "DELIVERY_BRANCH": "rehearsal/test",
            "EXPECTED_COMMIT": "a" * 40,
            "GITHUB_SHA": "a" * 40,
            "DEPLOY_APPROVED": "true",
        }
        self.assertEqual(run_guard(script, **good).returncode, 0)
        for override in (
            {"GITHUB_REF": "refs/heads/REHEARSAL/test"},
            {"EXPECTED_COMMIT": "a" * 39},
            {"EXPECTED_COMMIT": "b" * 40},
            {"DEPLOY_APPROVED": "false"},
        ):
            self.assertNotEqual(run_guard(script, **(good | override)).returncode, 0)
        text = (WORKFLOWS / filename).read_text()
        self.assertIn("vars.WEATHER_DELIVERY_BRANCH || github.event.repository.default_branch", text)
        self.assertLess(text.index("Confirm the deployment decision"), text.index("azure/login@v2"))
        self.assertLess(text.index("Verify the exact GitHub OIDC subject"), text.index("azure/login@v2"))


if __name__ == "__main__":
    unittest.main()
