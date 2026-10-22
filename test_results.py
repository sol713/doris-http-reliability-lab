# SPDX-License-Identifier: Apache-2.0
# Generated-by: OpenAI Codex (GPT-6)
"""Offline result lifecycle regressions; every run uses an isolated workdir."""

import contextlib
import ctypes
import datetime
import errno
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
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


class ProcessResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise unittest.SkipTest("process regressions need Python pidfd APIs")
        try:
            descriptor = os.pidfd_open(os.getpid())
        except OSError as exc:
            if exc.errno == errno.ENOSYS:
                raise unittest.SkipTest("process regressions need Linux pidfd support") from exc
            raise
        else:
            os.close(descriptor)

    def run_scenario(self, mode):
        result = subprocess.run(
            [sys.executable, "-W", "error", str(Path(__file__).resolve()),
             "--signal-worker", "scenario", mode],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finding = next(line for line in result.stdout.splitlines()
                       if line.startswith("PROCESS_FINDING "))
        cleanup = next(line for line in result.stdout.splitlines()
                       if line.startswith("PROCESS_CLEANUP "))
        cleaned = json.loads(cleanup.removeprefix("PROCESS_CLEANUP "))
        self.assertTrue(cleaned["parent_reaped"] and cleaned["child_reaped"])
        print(finding, flush=True)
        print(cleanup, flush=True)
        return json.loads(finding.removeprefix("PROCESS_FINDING "))

    def test_sigterm_parent_retains_exclusion_until_direct_child_exits(self):
        finding = self.run_scenario("SIGTERM")
        self.assertEqual(finding["parent_returncode"], -signal.SIGTERM)

    def test_sigkill_parent_retains_exclusion_until_direct_child_exits(self):
        finding = self.run_scenario("SIGKILL")
        self.assertEqual(finding["parent_returncode"], -signal.SIGKILL)

    def test_checked_timeout_reaps_direct_child_and_releases_lock(self):
        finding = self.run_scenario("timeout")
        self.assertEqual(finding["parent_returncode"], 0)
        self.assertEqual(finding["interrupted_status"], "failed")
        self.assertEqual(finding["error_type"], "TimeoutExpired")


def process_worker(mode, *arguments):
    """Local helpers execute in separate processes, never in the test runner."""
    script = str(Path(__file__).resolve())
    if mode == "child":
        socket_path, directory = arguments
        signal.alarm(8)  # A failing test cannot leave this simulated child alive.
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(5)
            channel.connect(socket_path)
            channel.sendall((json.dumps({"pid": os.getpid()}) + "\n").encode())
            with channel.makefile("rb") as messages:
                for command in messages:
                    if command == b"PING\n":
                        channel.sendall(b"ALIVE\n")
                    elif command == b"WRITE\n":
                        work = Path(directory) / ".lab"
                        run_id = json.loads((work / "results.json").read_text())["run_id"]
                        (work / "child-write").write_text(run_id)
                        channel.sendall((run_id + "\n").encode())
                    elif command == b"EXIT\n":
                        channel.sendall(b"EXITING\n")
                        return
        return
    if mode in {"parent", "contender"}:
        directory, *extra = arguments
        root = Path(directory)
        lab.ROOT, lab.WORK = root, root / ".lab"
        if mode == "contender":
            def action(report, save, output):
                (root / "entered").write_text(report["run_id"])
            try:
                lab.recorded_run(action)
            except RuntimeError as exc:
                if "another lab run owns" not in str(exc):
                    raise
                print("CONTENDER_REJECTED", flush=True)
                sys.exit(23)
            return
        socket_path, scenario = extra
        def action(report, save, output):
            report["phase"] = "controlled_child"
            save()
            command = [sys.executable, "-W", "error", script, "--signal-worker",
                       "child", socket_path, str(root)]
            if scenario != "timeout":
                lab.checked(command, 6, "controlled direct child")
                return
            original_spawn = subprocess.Popen
            def synchronized_spawn(*args, **kwargs):
                process = original_spawn(*args, **kwargs)
                try:
                    with socket.socket(socket.AF_UNIX) as arm:
                        arm.settimeout(3)
                        arm.connect(str(root / "arm.sock"))
                        arm.sendall(b"SPAWNED\n")
                        with arm.makefile("rb") as messages:
                            if messages.readline() != b"ARM\n":
                                raise RuntimeError("timeout handshake failed")
                except BaseException:
                    process.kill()
                    process.communicate(timeout=3)
                    raise
                return process
            # run() starts its real timeout after Popen returns. Only synchronize
            # creation here; its communicate/kill/wait behavior stays unchanged.
            with patch.object(subprocess, "Popen", synchronized_spawn):
                lab.checked(command, 0.05, "controlled direct child")
        try:
            lab.recorded_run(action)
        except subprocess.TimeoutExpired:
            if scenario != "timeout":
                raise
        return
    if mode != "scenario":
        raise ValueError(mode)
    # Only this short-lived scenario supervisor adopts its own orphaned child.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # Linux PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable test child reaping")
    scenario, = arguments
    check = unittest.TestCase()
    with tempfile.TemporaryDirectory(prefix="doris-process-test-") as directory:
        root = Path(directory)
        work = root / ".lab"
        work.mkdir()
        seed = b'{"run_id":"seed","status":"success","passed":6}\n'
        latest = work / "results.json"
        latest.write_bytes(seed)
        socket_path = str(root / "handshake.sock")
        parent = None
        child_pid = None
        child_fd = None
        child_reaped = False

        def contender():
            return subprocess.run(
                [sys.executable, "-W", "error", script, "--signal-worker",
                 "contender", directory],
                capture_output=True, text=True, timeout=3,
            )

        def result_bytes():
            return {str(path.relative_to(work)): path.read_bytes()
                    for path in [latest, *sorted((work / "runs").rglob("*.json"))]}

        try:
            with socket.socket(socket.AF_UNIX) as listener, socket.socket(socket.AF_UNIX) as arm_listener:
                listener.bind(socket_path)
                listener.listen(1)
                listener.settimeout(3)
                if scenario == "timeout":
                    arm_listener.bind(str(root / "arm.sock"))
                    arm_listener.listen(1)
                    arm_listener.settimeout(3)
                parent = subprocess.Popen(
                    [sys.executable, "-W", "error", script, "--signal-worker",
                     "parent", directory, socket_path, scenario],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                connection, _ = listener.accept()
                with connection, connection.makefile("rb") as messages:
                    connection.settimeout(3)
                    child_pid = json.loads(messages.readline())["pid"]
                    child_fd = os.pidfd_open(child_pid)
                    active_bytes = latest.read_bytes()
                    active = json.loads(active_bytes)
                    check.assertEqual(active["status"], "running")
                    original_history = work / "runs" / active["run_id"]
                    check.assertEqual((original_history / "previous-results.json").read_bytes(), seed)
                    if scenario == "timeout":
                        arm, _ = arm_listener.accept()
                        with arm:
                            arm.settimeout(3)
                            with arm.makefile("rb") as arm_messages:
                                check.assertEqual(arm_messages.readline(), b"SPAWNED\n")
                            arm.sendall(b"ARM\n")
                        parent_returncode = parent.wait(timeout=3)
                        check.assertTrue(select.select([child_fd], [], [], 1)[0])
                        # subprocess.run must have killed AND waited, so this
                        # supervisor must not inherit an unreaped timeout child.
                        with check.assertRaises(ChildProcessError):
                            os.waitpid(child_pid, os.WNOHANG)
                        child_reaped = True
                        interrupted_bytes = latest.read_bytes()
                        interrupted = json.loads(interrupted_bytes)
                        check.assertEqual(interrupted["status"], "failed")
                        check.assertEqual(interrupted["error"]["type"], "TimeoutExpired")
                    else:
                        parent.send_signal(getattr(signal, scenario))
                        parent_returncode = parent.wait(timeout=3)
                        connection.sendall(b"PING\n")
                        check.assertEqual(messages.readline(), b"ALIVE\n")
                        before = result_bytes()
                        rejected = contender()
                        check.assertEqual(rejected.returncode, 23, rejected.stdout + rejected.stderr)
                        check.assertIn("CONTENDER_REJECTED", rejected.stdout)
                        check.assertFalse((root / "entered").exists())
                        check.assertEqual(result_bytes(), before)
                        check.assertEqual(latest.read_bytes(), active_bytes)
                        connection.sendall(b"WRITE\n")
                        check.assertEqual(messages.readline().decode().strip(), active["run_id"])
                        connection.sendall(b"EXIT\n")
                        check.assertEqual(messages.readline(), b"EXITING\n")
                        check.assertTrue(select.select([child_fd], [], [], 3)[0])
                        check.assertEqual(os.waitpid(child_pid, 0)[1], 0)
                        child_reaped = True
                        interrupted_bytes = active_bytes
                        interrupted = active
                    stdout, stderr = parent.communicate(timeout=3)
                    check.assertEqual(stderr, "", stdout + stderr)
                    accepted = contender()
                    check.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                    new = json.loads(latest.read_text())
                    check.assertNotEqual(new["run_id"], active["run_id"])
                    check.assertEqual(new["status"], "success")
                    check.assertEqual((root / "entered").read_text(), new["run_id"])
                    archived = work / "runs" / new["run_id"] / "previous-results.json"
                    check.assertEqual(archived.read_bytes(), interrupted_bytes)
                    check.assertEqual((original_history / "previous-results.json").read_bytes(), seed)
                    finding = {
                        "mode": scenario,
                        "parent_returncode": parent_returncode,
                        "interrupted_status": interrupted["status"],
                        "error_type": interrupted.get("error", {}).get("type"),
                        "child_exited_and_reaped": child_reaped,
                        "contender_rejected_while_child_alive": True if scenario != "timeout" else None,
                        "latest_and_history_unchanged_on_rejection": True if scenario != "timeout" else None,
                        "contender_entered_after_child_exit": True,
                        "interrupted_and_seed_history_preserved_exactly": True,
                    }
                    print("PROCESS_FINDING " + json.dumps(finding, sort_keys=True), flush=True)
        finally:
            try:
                if parent is not None:
                    if parent.poll() is None:
                        parent.kill()
                    parent.wait(timeout=3)  # Ensure orphan adoption before reaping.
            finally:
                try:
                    if child_fd is not None:
                        try:
                            if not child_reaped:
                                if not select.select([child_fd], [], [], 0)[0]:
                                    signal.pidfd_send_signal(child_fd, signal.SIGKILL)
                                if not select.select([child_fd], [], [], 3)[0]:
                                    raise RuntimeError("owned test child did not exit")
                                try:
                                    os.waitpid(child_pid, 0)
                                except ChildProcessError:
                                    pass  # Its original parent already reaped it.
                                child_reaped = True
                        finally:
                            os.close(child_fd)
                finally:
                    try:
                        if parent is not None:
                            parent.communicate(timeout=3)
                    finally:
                        print("PROCESS_CLEANUP " + json.dumps({
                            "mode": scenario,
                            "parent_pid": parent.pid if parent is not None else None,
                            "parent_reaped": parent is not None and parent.returncode is not None,
                            "child_pid": child_pid,
                            "child_reaped": child_reaped,
                        }, sort_keys=True), flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--signal-worker"]:
        process_worker(*sys.argv[2:])
    else:
        unittest.main()
