import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator.swe_chain_evo import load_chain
from simulator.openhands.evo_tests import (
    _apply_test_patch,
    _target_status,
    run_required_tests,
    validate_required_verdict,
)
from simulator.openhands.judge import candidate_hash
from simulator.openhands.progressive import ProgressiveEpisode
from simulator.openhands.state import TaskState, TransitionError


class SweChainEvoTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    def repository(self, root):
        repo = root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "code.py").write_text("value = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        base = self.git(repo, "rev-parse", "HEAD")
        (repo / "code.py").write_text("value = 2\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "target",
            ],
            check=True,
        )
        return repo, base, self.git(repo, "rev-parse", "HEAD")

    def test_pinned_manifest_and_instance_are_adapted_without_parent_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base, target = self.repository(root)
            chain = "owner__repo_multistep_v25_L05_s0"
            instance_id = "owner__repo_lc_0001_deadbeef"
            manifest = dict(
                chain_id=chain,
                repo="owner/repo",
                instance_count=1,
                instances=[
                    dict(instance_id=instance_id, version_chain_index=0)
                ],
            )
            f2p, p2p = ["tests/test_x.py::test_new"], ["tests/test_x.py::test_old"]
            instance = dict(
                instance_id=instance_id,
                version_chain_id=chain,
                version_chain_index=0,
                problem_statement="Implement the milestone.",
                repo="owner/repo",
                fail_to_pass=f2p,
                pass_to_pass=p2p,
                raw=dict(
                    repo="owner/repo",
                    base_commit=base,
                    target_commit=target,
                    patch="diff --git a/code.py b/code.py\n",
                    test_patch="diff --git a/tests/test_x.py b/tests/test_x.py\n",
                    test_cmds=["python -m pytest -rA tests/test_x.py"],
                    target_test_names=["tests/test_x.py::test_new"],
                    p2p_guard_tests=["tests/test_x.py::test_old"],
                    FAIL_TO_PASS=f2p,
                    PASS_TO_PASS=p2p,
                ),
            )
            files = {
                f"chains/{chain}/chain.json": manifest,
                f"chains/{chain}/instances/000-{instance_id}.json": instance,
            }

            def download(_source, filename):
                path = root / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(files[filename]))
                return path

            config = dict(
                swe_chain_evo=dict(
                    repo_id="Aiden0526/SWE-Chain-Evo",
                    revision="f" * 40,
                    chain_id=chain,
                    local_dir=str(root / "cache"),
                    limit=1,
                )
            )
            resolved_base, tasks = load_chain(config, repo, download=download)
            self.assertEqual(resolved_base, base)
            self.assertEqual(tasks[0]["base"], base)
            self.assertEqual(tasks[0]["reference"], target)
            self.assertEqual(tasks[0]["body"], "Implement the milestone.")
            self.assertEqual(tasks[0]["test_cmds"], instance["raw"]["test_cmds"])

    def test_patch_application_is_confined_and_changes_the_copy(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            path = root / "tests/test_x.py"
            path.write_text("value = 1\n")
            patch = """diff --git a/tests/test_x.py b/tests/test_x.py
--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
            _apply_test_patch(root, patch)
            self.assertEqual(path.read_text(), "value = 2\n")

    def test_required_patch_overlays_authoritative_tests_over_candidate_edits(self):
        from simulator.openhands.evo_tests import _install_test_patch

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            base = root / "base"
            candidate = root / "candidate"
            base.mkdir()
            candidate.mkdir()
            (base / "tests").mkdir()
            (candidate / "tests").mkdir()
            (base / "tests/test_x.py").write_text("value = 1\n")
            (candidate / "tests/test_x.py").write_text("value = 'weakened'\n")
            patch = """diff --git a/tests/test_x.py b/tests/test_x.py
--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
            changed = _install_test_patch(candidate, base, patch)
            self.assertEqual(changed, ["tests/test_x.py"])
            self.assertEqual((candidate / "tests/test_x.py").read_text(), "value = 2\n")

    def test_parameter_matching_does_not_match_similar_test_names(self):
        rows = [("passed", "tests/test_x.py::test_foobar")]
        self.assertEqual(_target_status("tests/test_x.py::test_foo", rows), "missing")
        rows.append(("passed", "tests/test_x.py::test_foo[value]"))
        self.assertEqual(_target_status("tests/test_x.py::test_foo", rows), "passed")

    def test_unknown_tests_cannot_be_called_solved_or_unsolved(self):
        for outcome in ("solved", "unsolved"):
            with self.assertRaises(ValueError):
                validate_required_verdict({"outcome": "unknown"}, outcome)
        validate_required_verdict({"outcome": "unknown"}, "uncertain")
        with self.assertRaises(ValueError):
            validate_required_verdict({"outcome": "failed"}, "solved")
        validate_required_verdict({"outcome": "passed"}, "solved")

    def test_fixed_runner_records_real_commands_in_an_isolated_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidate"
            source.mkdir()
            (source / "code.py").write_text("candidate\n")
            experiments = root / "experiments"
            experiments.mkdir()
            task = dict(
                identifier="instance-1",
                test_patch="private tests",
                test_cmds=["python -m pytest -rA tests/test_x.py"],
                fail_to_pass=["tests/test_x.py::test_new"],
                pass_to_pass=["tests/test_x.py::test_old"],
            )

            class Result:
                returncode = 0
                stdout = (
                    "collected 2 items\n"
                    "PASSED tests/test_x.py::test_new\n"
                    "PASSED tests/test_x.py::test_old\n"
                )

            sandbox = type("Sandbox", (), {"name": "judge-sandbox"})()
            with patch(
                "simulator.openhands.evo_tests.subprocess.run", return_value=Result()
            ) as execute:
                record = run_required_tests(
                    sandbox,
                    source,
                    experiments,
                    task,
                    "candidate-r1",
                    "candidate",
                    apply_test_patch=False,
                )
            self.assertEqual(record["outcome"], "passed")
            self.assertEqual(record["collected"], 2)
            self.assertEqual(record["candidate_version"], candidate_hash(source))
            self.assertEqual((source / "code.py").read_text(), "candidate\n")
            self.assertEqual(
                (experiments / "candidate-r1/code.py").read_text(), "candidate\n"
            )
            self.assertTrue((experiments / "candidate-r1/result.json").is_file())
            args = execute.call_args.args[0]
            self.assertIn("/workspace/experiments/candidate-r1", args)
            self.assertEqual(args[-1], task["test_cmds"][0])

    def test_timeout_pauses_and_records_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidate"
            source.mkdir()
            (source / "code.py").write_text("candidate\n")
            experiments = root / "experiments"
            experiments.mkdir()
            task = dict(
                identifier="instance-1",
                test_patch="private tests",
                test_cmds=["python -m pytest tests/test_x.py"],
                fail_to_pass=["tests/test_x.py::test_new"],
                pass_to_pass=["tests/test_x.py::test_old"],
            )

            class Sandbox:
                name = "judge-sandbox"
                paused = False

                def pause(self):
                    self.paused = True

            sandbox = Sandbox()
            timeout = subprocess.TimeoutExpired(
                task["test_cmds"][0], 10, output=b"partial output\n"
            )
            with patch(
                "simulator.openhands.evo_tests.subprocess.run", side_effect=timeout
            ):
                with self.assertRaises(RuntimeError):
                    run_required_tests(
                        sandbox,
                        source,
                        experiments,
                        task,
                        "candidate-timeout",
                        "candidate",
                        apply_test_patch=False,
                    )
            self.assertTrue(sandbox.paused)
            result = json.loads(
                (experiments / "candidate-timeout/result.json").read_text()
            )
            self.assertEqual(result["outcome"], "unknown")
            self.assertTrue(result["commands"][0]["timed_out"])
            self.assertEqual(
                (experiments / "candidate-timeout/test-1.log").read_text(),
                "partial output\n",
            )

    def test_acceptance_requires_candidate_bound_required_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            episode = ProgressiveEpisode.__new__(ProgressiveEpisode)
            episode.root = Path(directory)
            candidate = episode.root / "workspace/candidate"
            candidate.mkdir(parents=True)
            (candidate / "code.py").write_text("value = 1\n")
            episode.state = TaskState()
            episode.saved = {
                "revision": 1,
                "tasks": [{"kind": "swe_chain_evo"}],
            }
            episode.progress = {
                "tasks": {
                    "task-1": {
                        "verdict": {
                            "outcome": "solved",
                            "revision": 1,
                            "candidate_version": candidate_hash(candidate),
                            "required_tests": "failed",
                        }
                    }
                }
            }
            with self.assertRaises(TransitionError):
                episode.acceptance_gate()
            episode.current()["verdict"]["required_tests"] = "passed"
            episode.acceptance_gate()


if __name__ == "__main__":
    unittest.main()
