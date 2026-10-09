# SPDX-License-Identifier: Apache-2.0
# Generated-by: OpenAI Codex (GPT-6)
"""Guard tests for source integrity, finite budgets, and network boundaries."""

import math
import unittest

import lab
from cases import require_loopback


class Guards(unittest.TestCase):
    def test_changed_source_is_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            lab.load_http(b"raise AssertionError('must not execute')")

    def test_runtime_patch_matches_previously_tested_module(self):
        import hashlib

        fixed = lab.runtime_fix(lab.SOURCE.read_bytes())
        self.assertEqual(hashlib.sha256(fixed).hexdigest(), lab.FIXED_SHA256)

    def test_finite_cap_and_non_finite_fallback(self):
        fixed = lab.load_http(lab.SOURCE.read_bytes(), patched=True)
        for value in (math.nan, math.inf, -math.inf):
            self.assertEqual(fixed._bounded_float(value, 0.04), 0.04)
        self.assertEqual(fixed._bounded_float(90, 0.04), 60)
        self.assertEqual(fixed._bounded_float(0.08, 0.04), 0.08)

    def test_connection_guard_accepts_only_exact_ipv4_loopback(self):
        require_loopback(("127.0.0.1", 8030))
        for host in ("127.0.0.2", "::1", "169.254.169.254", "example.com"):
            with self.assertRaises(OSError):
                require_loopback((host, 8030))


if __name__ == "__main__":
    unittest.main()
