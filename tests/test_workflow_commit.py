import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "monitor.yml"
STATE_FILES = ["docs/fetch_state.json", "docs/missav_guard_state.json",
               "docs/notified_ids.json", "docs/notify_queue.json"]


def check_steps():
    """Split the check job into step blocks keyed by name (no PyYAML dependency)."""
    text = WORKFLOW.read_text(encoding="utf-8")
    job = text.split("\n  check:\n", 1)[1].split("\n  pages:\n", 1)[0]
    steps = {}
    for block in re.split(r"\n      - ", job)[1:]:
        match = re.search(r"(?m)^(?:      )?name: (.+)$", block)
        if match:
            steps[match.group(1).strip()] = block
    return steps


def condition(block):
    match = re.search(r"(?m)^        if: (.+)$", block)
    return match.group(1).strip() if match else ""


class WorkflowCommitTests(unittest.TestCase):
    def setUp(self):
        self.steps = check_steps()

    def test_site_commit_runs_only_on_success(self):
        block = self.steps["Commit site data"]
        self.assertEqual(condition(block), "")
        self.assertNotIn("always()", block)
        self.assertIn("git add docs", block)

    def test_failure_step_commits_only_explicit_state_files(self):
        block = self.steps["Preserve state files after failure"]
        self.assertEqual(condition(block), "failure()")
        loop = re.search(r"for f in (.+?); do", block).group(1).split()
        self.assertEqual(loop, STATE_FILES)
        self.assertNotIn("git add docs", block)
        for excluded in ("crawl_state.json", "data.json", "index.html", "catalog"):
            self.assertNotIn(excluded, loop and " ".join(loop))
        adds = re.findall(r"git add\b[^\n]*", block)
        self.assertEqual(adds, ['git add -- "$f"; fi'])
        # Must re-base on the run's start commit, never overwrite a newer origin/main.
        self.assertIn('git reset -q "$START_SHA"', block)
        self.assertNotIn("reset -q origin/main", block)
        self.assertNotIn("--force", block)
        record = self.steps["Record start commit"]
        self.assertIn('START_SHA=$(git rev-parse HEAD)', record)
        self.assertIn('"$GITHUB_ENV"', record)
        self.assertLess(list(self.steps).index("Record start commit"),
                        list(self.steps).index("Verify update and parser regressions"))

    def test_failure_step_follows_site_commit_and_precedes_pages(self):
        names = list(self.steps)
        self.assertLess(names.index("Commit site data"), names.index("Preserve state files after failure"))
        self.assertLess(names.index("Preserve state files after failure"),
                        names.index("Prepare lean Pages artifact"))
        for name in names[names.index("Preserve state files after failure") + 1:]:
            self.assertNotIn("always()", self.steps[name])

    def test_pages_artifact_excludes_notify_queue(self):
        self.assertIn("pages-public/notify_queue.json", self.steps["Prepare lean Pages artifact"])


if __name__ == "__main__":
    unittest.main()
