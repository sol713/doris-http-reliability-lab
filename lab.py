# SPDX-License-Identifier: Apache-2.0
# Generated-by: OpenAI Codex (GPT-6)
"""Bootstrap and run a pinned, loopback-only Doris HTTP reliability lab."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

BASE_COMMIT = "5daf1deb26bc0db02c19bf5ca1d070acea4cfab9"
SOURCE_URL = (
    "https://raw.githubusercontent.com/apache/doris-mcp-server/"
    f"{BASE_COMMIT}/doris_mcp_server/utils/doris_http_client.py"
)
BASE_SHA256 = "ef2ec7ea1ad8fd9f734a38516202b15e0eb7c4d852f3a22bc2b55cd1630ec115"
FIXED_SHA256 = "898bcd7baaeaa37c04de88bb98ad95b278257451dfc8268b05b1f78941ea357f"
ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".lab"
SOURCE = WORK / "upstream_http.py"
OLD = (
    "    if parsed <= 0:\n"
    "        return default\n"
    "    return min(parsed, MAX_TIMEOUT_SECONDS)\n"
)
NEW = OLD.replace("if parsed <= 0:", "if not math.isfinite(parsed) or parsed <= 0:")


def verify_source(data: bytes) -> None:
    if hashlib.sha256(data).hexdigest() != BASE_SHA256:
        raise ValueError("pinned upstream source SHA-256 mismatch")


def runtime_fix(data: bytes) -> bytes:
    verify_source(data)
    text = data.decode("utf-8")
    if text.count(OLD) != 1 or text.count("import ipaddress\n") != 1:
        raise ValueError("expected upstream patch context is absent")
    fixed = text.replace("import ipaddress\n", "import ipaddress\nimport math\n", 1)
    fixed = fixed.replace(OLD, NEW, 1).encode("utf-8")
    if hashlib.sha256(fixed).hexdigest() != FIXED_SHA256:
        raise ValueError("runtime patch SHA-256 mismatch")
    return fixed


def load_http(data: bytes, *, patched: bool = False) -> types.ModuleType:
    verify_source(data)  # Reject changed bytes before any source execution.
    if patched:
        data = runtime_fix(data)
        data += b"\n# Lab modification: finite timeout normalization; see README.\n"
    name = "lab_http_fixed" if patched else "lab_http_baseline"
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(data, "upstream_http.py", "exec"), module.__dict__)
    return module


def fetch_source() -> None:
    WORK.mkdir(exist_ok=True)
    # This setup download is separate from the loopback-only experiment phase.
    if SOURCE.exists():
        verify_source(SOURCE.read_bytes())
        print("SOURCE cached: pinned commit and SHA-256 verified", flush=True)
        return
    with urllib.request.urlopen(SOURCE_URL, timeout=20) as response:
        if response.geturl() != SOURCE_URL:
            raise ValueError("unexpected upstream download redirect")
        data = response.read(100_001)
    if len(data) > 100_000:
        raise ValueError("upstream source exceeded download limit")
    verify_source(data)
    SOURCE.write_bytes(data)
    print("SOURCE downloaded: pinned commit and SHA-256 verified", flush=True)


def clean_environment() -> dict[str, str]:
    # Use the public index explicitly; do not inherit pip credentials/configuration.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PIP_")
        and key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"}
    }
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PYTHONUNBUFFERED"] = "1"
    return env


def checked(command: list[str], timeout: int, label: str) -> None:
    result = subprocess.run(
        command, cwd=ROOT, env=clean_environment(), timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError(f"{label} failed (exit {result.returncode})")


def run_lab() -> None:
    from cases import run_cases

    data = SOURCE.read_bytes()
    baseline, fixed = load_http(data), load_http(data, patched=True)
    rows = asyncio.run(run_cases(baseline, fixed), debug=True)
    dependencies = {}
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        if "==" in line and not line.startswith("#"):
            name, pin = line.split("==", 1)
            pin = pin.split()[0]
            actual = importlib.metadata.version(name)
            if actual != pin:
                raise ValueError(f"dependency version mismatch: {name}")
            dependencies[name] = actual
    report = {
        "lab_version": "0.1.0",
        "recorded_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "baseline_commit": BASE_COMMIT,
        "source_url": SOURCE_URL,
        "baseline_sha256": BASE_SHA256,
        "runtime_patch_sha256": FIXED_SHA256,
        "python": platform.python_version(),
        "dependencies": dependencies,
        "experiment_network": "127.0.0.1 only; synthetic DNS",
        "generated_by": "OpenAI Codex (GPT-6)",
        "cases": rows,
        "passed": len(rows),
    }
    (WORK / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    for row in rows:
        print(
            f"PASS {row['case']}: {row['classification']} ({row['elapsed_ms']:.1f} ms)"
        )
    print(f"{len(rows)} loopback cases passed; JSON written to .lab/results.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fetch:
        fetch_source()
        return
    if args.run:
        if sys.flags.optimize:
            raise RuntimeError("experiment checks require Python assertions enabled")
        run_lab()
        return
    if (
        sys.version_info[:2] != (3, 12)
        or platform.python_implementation() != "CPython"
        or sys.platform != "linux"
        or platform.machine() != "x86_64"
    ):
        raise RuntimeError("supported bootstrap: CPython 3.12 on Linux x86_64")
    WORK.mkdir(exist_ok=True)
    interpreter = WORK / "venv" / "bin" / "python"
    if not interpreter.exists():
        # venv creation runs in a bounded child as well as dependency installation.
        checked([sys.executable, "-m", "venv", str(WORK / "venv")], 60, "venv")
    print("SETUP: public PyPI, fixed binary wheels and SHA-256 hashes", flush=True)
    checked(
        [
            str(interpreter),
            "-m",
            "pip",
            "install",
            "--index-url",
            "https://pypi.org/simple",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--timeout",
            "20",
            "--retries",
            "0",
            "-r",
            "requirements.lock",
        ],
        180,
        "pinned dependency install",
    )
    checked(
        [str(interpreter), "-m", "pip", "--no-cache-dir", "check"],
        20,
        "dependency check",
    )
    checked([str(interpreter), "lab.py", "--fetch"], 45, "source download")
    checked([str(interpreter), "-W", "error", "lab.py", "--run"], 30, "loopback cases")
    checked(
        [str(interpreter), "-W", "error", "-m", "unittest", "-q", "test_lab"],
        20,
        "guard tests",
    )
    print("DONE: 6 cases and 4 guard tests passed", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"LAB FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
