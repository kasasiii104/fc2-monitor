import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "monitor.yml"
STATE_FILES = ["docs/fetch_state.json", "docs/missav_guard_state.json",
               "docs/notified_ids.json", "docs/notify_queue.json", "docs/source_cooldowns.json"]


def check_steps(workflow=WORKFLOW, job_name="check"):
    """Split the named job into step blocks keyed by name (no PyYAML dependency)."""
    text = workflow.read_text(encoding="utf-8")
    job = text.split(f"\n  {job_name}:\n", 1)[1].split("\n  pages:\n", 1)[0]
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

    def test_archive_advance_skips_all_sources_denied_run(self):
        discovery = self.steps["Fast MissAV recent and archive discovery"]
        self.assertRegex(discovery, r"(?m)^        id: fast_discovery$")
        advance = self.steps["Advance past confirmed duplicate archive page"]
        self.assertRegex(
            advance,
            r"(?m)^        if: >-\n          \(.*(?:\n          .*?)*\) &&\n"
            r"          steps\.fast_discovery\.outputs\.all_sources_denied != 'true'$",
        )

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
        self.assertEqual(adds, ['git add -- "$f"; fi', 'git add -A -- docs/fetch_state-shards; fi'])
        self.assertIn(
            "          done\n"
            "          if [ -d docs/fetch_state-shards ] || "
            "git ls-files --error-unmatch -- docs/fetch_state-shards >/dev/null 2>&1; "
            "then git add -A -- docs/fetch_state-shards; fi\n",
            block,
        )
        # Must re-base on the run's start commit, never overwrite a newer origin/main.
        self.assertIn('git reset -q "$START_SHA"', block)
        self.assertNotIn("reset -q origin/main", block)
        self.assertNotIn("--force", block)
        record = self.steps["Record start commit"]
        self.assertIn('START_SHA=$(git rev-parse HEAD)', record)
        self.assertIn('"$GITHUB_ENV"', record)
        self.assertLess(list(self.steps).index("Record start commit"),
                        list(self.steps).index("Verify update and parser regressions"))

    def test_preserve_step_stages_shard_additions_and_deletions(self):
        if shutil.which("git") is None or shutil.which("bash") is None:
            self.skipTest("git and bash are required")
        block = self.steps["Preserve state files after failure"]
        command = re.search(r"(?m)^ +(if .*git add -A -- docs/fetch_state-shards; fi)$", block).group(1)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def run(*args):
            return subprocess.check_output(args, cwd=root, text=True).strip()

        run("git", "init", "-q")
        shard_dir = root / "docs" / "fetch_state-shards"
        shard_dir.mkdir(parents=True)
        old = shard_dir / "old.json"
        new = shard_dir / "new.json"
        old.write_text("{}")
        run("git", "add", "--", str(old))
        baseline = run("git", "write-tree")

        new.write_text("{}")
        old.unlink()
        run("bash", "-ec", command)
        self.assertCountEqual(
            run("git", "diff", "--cached", "--name-status", "--no-renames", baseline).splitlines(),
            ["A\tdocs/fetch_state-shards/new.json", "D\tdocs/fetch_state-shards/old.json"],
        )

        baseline = run("git", "write-tree")
        new.unlink()
        shard_dir.rmdir()
        run("bash", "-ec", command)
        self.assertEqual(
            run("git", "diff", "--cached", "--name-status", baseline),
            "D\tdocs/fetch_state-shards/new.json",
        )

    def test_failure_step_follows_site_commit_and_precedes_pages(self):
        names = list(self.steps)
        self.assertLess(names.index("Commit site data"), names.index("Preserve state files after failure"))
        self.assertLess(names.index("Preserve state files after failure"),
                        names.index("Prepare lean Pages artifact"))
        for name in names[names.index("Preserve state files after failure") + 1:]:
            self.assertNotIn("always()", self.steps[name])

    def test_pages_artifact_excludes_notify_queue(self):
        self.assertIn("pages-public/notify_queue.json", self.steps["Prepare lean Pages artifact"])

    def test_pages_artifacts_exclude_state_shards(self):
        for workflow, job_name in ((WORKFLOW, "check"), (WORKFLOW.with_name("compact-pages.yml"), "compact")):
            with self.subTest(workflow=workflow.name):
                block = check_steps(workflow, job_name)["Prepare lean Pages artifact"]
                self.assertRegex(block, r"(?m)^          rm -rf pages-public/data-shards$")
                self.assertRegex(block, r"(?m)^          rm -rf pages-public/fetch_state-shards$")

    def test_pages_artifacts_exclude_source_cooldowns(self):
        for workflow, job_name in ((WORKFLOW, "check"), (WORKFLOW.with_name("compact-pages.yml"), "compact")):
            with self.subTest(workflow=workflow.name):
                block = check_steps(workflow, job_name)["Prepare lean Pages artifact"]
                shell = block.replace("\\\n", "")
                remove_files = re.search(r"(?m)^ +rm -f (.+)$", shell).group(1).split()
                self.assertIn("pages-public/source_cooldowns.json", remove_files)


if __name__ == "__main__":
    unittest.main()
