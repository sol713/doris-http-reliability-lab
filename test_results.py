# SPDX-License-Identifier: Apache-2.0
# Generated-by: OpenAI Codex (GPT-6)
"""Offline result lifecycle regressions; every run uses an isolated workdir."""

import contextlib
import datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import lab


class Results(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="doris-results-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / ".lab"
        self.work.mkdir()
        self.latest = self.work / "results.json"
        self.source = self.work / "upstream_http.py"
        self.source.write_bytes(b"controlled changed source")
        (self.root / "requirements.lock").write_text("")
        for attribute, value in (
            ("ROOT", self.root), ("WORK", self.work), ("SOURCE", self.source)
        ):
            replacement = patch.object(lab, attribute, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        # Hold no result lock and touch no actual checkout state.
        self.old_bytes = b'{"passed":6,"cases":[{"case":"old-success"}]}\n'
        self.latest.write_bytes(self.old_bytes)
        self.case_report = {
            "recorded_at_utc": lab.utc_now(),
            "baseline_commit": lab.BASE_COMMIT,
            "cases": [
                {"case": f"controlled_{i}", "classification": "controlled pass", "elapsed_ms": 0.0}
                for i in range(6)
            ],
            "passed": 6,
        }

    def read_latest(self):
        return json.loads(self.latest.read_text())

    def run_main(self, *arguments):
        with patch.object(sys, "argv", ["lab.py", *arguments]), contextlib.redirect_stdout(io.StringIO()):
            lab.main()

    def controlled_check(self, command, timeout, label):
        if label == "loopback cases":
            output = Path(command[command.index("--case-output") + 1])
            lab.atomic_json(output, self.case_report)

    def fail_stage(self, label, error):
        def checked(command, timeout, actual_label):
            if actual_label == label:
                raise error
            self.controlled_check(command, timeout, actual_label)
        return checked

    def assert_failed(self, phase, error_type):
        report = self.read_latest()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["scope"], "full")
        self.assertEqual(report["phase"], phase)
        self.assertEqual(report["error"]["type"], error_type)
        self.assertNotIn("old-success", self.latest.read_text())
        run_dir = self.work / "runs" / report["run_id"]
        self.assertEqual((run_dir / "previous-results.json").read_bytes(), self.old_bytes)
        self.assertEqual(json.loads((run_dir / "results.json").read_text()), report)
        return report

    def test_setup_failure_replaces_old_success(self):
        with patch.object(lab, "checked", side_effect=self.fail_stage(
            "pinned dependency install", RuntimeError("controlled install failure")
        )):
            with self.assertRaisesRegex(RuntimeError, "install failure"):
                self.run_main()
        report = self.assert_failed("dependency_install", "RuntimeError")
        self.assertEqual(report["cases"], [])
        self.assertEqual(report["passed"], 0)

    def test_hash_failure_uses_real_source_verification(self):
        def checked(command, timeout, label):
            if label == "source download":
                lab.fetch_source()
            self.controlled_check(command, timeout, label)
        with patch.object(lab, "checked", side_effect=checked):
            with self.assertRaisesRegex(ValueError, "pinned upstream source SHA-256 mismatch"):
                self.run_main()
        self.assert_failed("source_download", "ValueError")

    def test_case_assertion_failure_cannot_retain_old_success(self):
        async def failed_cases(*args):
            raise AssertionError("controlled case failure")
        module = types.ModuleType("cases")
        module.run_cases = failed_cases
        def checked(command, timeout, label):
            if label == "loopback cases":
                lab.run_lab()
            self.controlled_check(command, timeout, label)
        with patch.dict(sys.modules, {"cases": module}), patch.object(
            lab, "load_http", return_value=object()
        ), patch.object(lab, "checked", side_effect=checked):
            with self.assertRaisesRegex(AssertionError, "controlled case failure"):
                self.run_main()
        self.assert_failed("cases", "AssertionError")

    def test_guard_failure_retains_only_current_case_evidence(self):
        with patch.object(lab, "checked", side_effect=self.fail_stage(
            "guard tests", RuntimeError("controlled guard failure (exit 1)")
        )):
            with self.assertRaisesRegex(RuntimeError, "guard failure"):
                self.run_main()
        report = self.assert_failed("guard_tests", "RuntimeError")
        self.assertEqual(report["cases"], self.case_report["cases"])
        self.assertEqual(report["passed"], 6)
        raw_cases = self.work / "runs" / report["run_id"] / "cases.json"
        self.assertEqual(json.loads(raw_cases.read_text()), self.case_report)

    def test_unsupported_runtime_is_a_current_failure(self):
        with patch.object(lab.platform, "machine", return_value="unsupported"), patch.object(lab, "checked") as checked:
            with self.assertRaisesRegex(RuntimeError, "supported bootstrap"):
                self.run_main()
        checked.assert_not_called()
        self.assert_failed("runtime", "RuntimeError")

    def test_child_timeout_is_recorded_before_reraise(self):
        with patch.object(lab, "checked", side_effect=self.fail_stage(
            "loopback cases", subprocess.TimeoutExpired(["controlled-child"], 30)
        )):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_main()
        report = self.assert_failed("cases", "TimeoutExpired")
        self.assertIn("30", report["error"]["message"])

    def test_result_stays_running_through_guards_and_then_succeeds(self):
        phases = []
        def checked(command, timeout, label):
            current = self.read_latest()
            self.assertEqual(current["status"], "running")
            self.assertNotIn("finished_at_utc", current)
            phases.append((label, current["phase"]))
            if label == "guard tests":
                self.assertEqual(current["passed"], 6)
                self.assertEqual(current["cases"], self.case_report["cases"])
            self.controlled_check(command, timeout, label)
        with patch.object(lab, "checked", side_effect=checked):
            self.run_main()
        self.assertIn(("guard tests", "guard_tests"), phases)
        report = self.read_latest()
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["phase"], "complete")
        self.assertEqual(report["scope"], "full")
        self.assertEqual(report["passed"], 6)

    def test_consecutive_runs_have_distinct_ids_real_utc_and_exact_history(self):
        before = datetime.datetime.now(datetime.timezone.utc)
        with patch.object(lab, "checked", side_effect=self.controlled_check):
            self.run_main()
            first_bytes = self.latest.read_bytes()
            first = self.read_latest()
            self.run_main()
        second = self.read_latest()
        after = datetime.datetime.now(datetime.timezone.utc)
        self.assertNotEqual(first["run_id"], second["run_id"])
        for report in (first, second):
            start = datetime.datetime.fromisoformat(report["started_at_utc"])
            finish = datetime.datetime.fromisoformat(report["finished_at_utc"])
            self.assertEqual(start.utcoffset(), datetime.timedelta(0))
            self.assertEqual(finish.utcoffset(), datetime.timedelta(0))
            self.assertLessEqual(before, start)
            self.assertLessEqual(start, finish)
            self.assertLessEqual(finish, after)
        runs = self.work / "runs"
        self.assertEqual((runs / first["run_id"] / "previous-results.json").read_bytes(), self.old_bytes)
        self.assertEqual((runs / second["run_id"] / "previous-results.json").read_bytes(), first_bytes)
        self.assertEqual((runs / first["run_id"] / "results.json").read_bytes(), first_bytes)

    def test_atomic_replace_failure_preserves_complete_target_and_cleans_temp(self):
        before_paths = set(self.work.iterdir())
        with patch.object(lab.os, "replace", side_effect=OSError("controlled replace failure")):
            with self.assertRaisesRegex(OSError, "replace failure"):
                lab.atomic_json(self.latest, {"new": "must not partially overwrite"})
        self.assertEqual(self.latest.read_bytes(), self.old_bytes)
        self.assertEqual(set(self.work.iterdir()), before_paths)

    def test_atomic_serialization_failure_preserves_target_and_cleans_temp(self):
        before_paths = set(self.work.iterdir())
        with self.assertRaises(TypeError):
            lab.atomic_json(self.latest, {"not_json": object()})
        self.assertEqual(self.latest.read_bytes(), self.old_bytes)
        self.assertEqual(set(self.work.iterdir()), before_paths)

    def test_archive_failure_stops_before_body_and_keeps_old_bytes(self):
        action = Mock()
        with patch.object(lab.os, "replace", side_effect=OSError("controlled archive failure")):
            with self.assertRaisesRegex(OSError, "archive failure"):
                lab.recorded_run(action)
        action.assert_not_called()
        self.assertEqual(self.latest.read_bytes(), self.old_bytes)

    def test_initial_publication_failure_stops_before_body_and_keeps_archive(self):
        action = Mock()
        with patch.object(lab, "atomic_json", side_effect=OSError("controlled write failure")):
            with self.assertRaisesRegex(OSError, "write failure"):
                lab.recorded_run(action)
        action.assert_not_called()
        self.assertFalse(self.latest.exists())
        archives = list((self.work / "runs").glob("*/previous-results.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), self.old_bytes)

    def test_contending_run_cannot_replace_active_result(self):
        def action(report, save, output):
            active_bytes = self.latest.read_bytes()
            contender = Mock()
            with self.assertRaisesRegex(RuntimeError, "another lab run owns"):
                lab.recorded_run(contender)
            contender.assert_not_called()
            self.assertEqual(self.latest.read_bytes(), active_bytes)
            self.assertEqual(len(list((self.work / "runs").iterdir())), 1)
        lab.recorded_run(action)
        self.assertEqual(self.read_latest()["status"], "success")

    def test_interruption_is_recorded_and_lock_is_released(self):
        def interrupted(report, save, output):
            report["phase"] = "controlled_interrupt"
            raise KeyboardInterrupt("controlled interrupt")
        with self.assertRaises(KeyboardInterrupt):
            lab.recorded_run(interrupted)
        self.assert_failed("controlled_interrupt", "KeyboardInterrupt")
        lab.recorded_run(lambda report, save, output: None)
        self.assertEqual(self.read_latest()["status"], "success")

    def test_direct_run_cannot_claim_full_run_success(self):
        with patch.object(lab, "run_lab", return_value=self.case_report), patch.object(lab, "checked") as checked:
            self.run_main("--run")
        checked.assert_not_called()
        report = self.read_latest()
        self.assertEqual(report["status"], "cases_passed")
        self.assertEqual(report["scope"], "cases_only")
        self.assertEqual(report["passed"], 6)

    def test_internal_case_child_does_not_publish_parent_latest(self):
        case_output = self.work / "controlled-cases.json"
        with patch.object(lab, "run_lab", return_value=self.case_report):
            self.run_main("--run", "--case-output", str(case_output))
        self.assertEqual(self.latest.read_bytes(), self.old_bytes)
        self.assertEqual(json.loads(case_output.read_text()), self.case_report)
        self.assertFalse((self.work / "runs").exists())


if __name__ == "__main__":
    unittest.main()
