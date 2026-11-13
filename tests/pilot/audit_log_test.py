# Copyright 2026 ads-pilot-mcp contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for ads_mcp.safety.audit_log."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ads_mcp.safety import audit_log


class TestAuditLog(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "nested", "audit.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_append_creates_jsonl_and_only_appends(self):
        log = audit_log.AuditLog(self.path)
        self.assertTrue(log.append({"event": "COMMITTED", "tool": "a"}))
        self.assertTrue(log.append({"event": "BLOCKED", "tool": "b"}))
        with open(self.path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["tool"], "a")
        self.assertRegex(first["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual([e["tool"] for e in log.read()], ["a", "b"])

        # A second AuditLog on the same file keeps appending.
        audit_log.AuditLog(self.path).append({"event": "FAILED", "tool": "c"})
        self.assertEqual(len(list(log.read())), 3)

    def test_redaction(self):
        entry = {
            "developer_token": "abc",
            "nested": {"client_secret": "x", "refresh_token": "y", "ok": 1},
            "note": "Bearer ya29.FAKE-access-token and 1//FAKE-refresh-token-for-tests-only",
            "jwt": "eyJGQUtF.eyJGQUtF.RkFLRQ",
            "items": [{"Authorization": "Bearer z"}],
            "primary_key_readable": "customers/1234567890/labels/1",
        }
        redacted = audit_log.redact(entry)
        self.assertEqual(redacted["developer_token"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["client_secret"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["refresh_token"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["ok"], 1)
        self.assertNotIn("ya29.", redacted["note"])
        self.assertNotIn("1//FAKE", redacted["note"])
        self.assertEqual(redacted["jwt"], "[REDACTED]")
        self.assertEqual(redacted["items"][0]["Authorization"], "[REDACTED]")
        # Not a secret: kept as is.
        self.assertEqual(
            redacted["primary_key_readable"], "customers/1234567890/labels/1"
        )

    def test_truncation_and_bytes(self):
        redacted = audit_log.redact(
            {"long": "x" * 1000, "blob": b"\x00" * 50, "obj": object()}
        )
        self.assertLess(len(redacted["long"]), 400)
        self.assertTrue(redacted["long"].endswith("(truncated)"))
        self.assertEqual(redacted["blob"], "<50 bytes>")
        self.assertIsInstance(redacted["obj"], str)

    def test_written_file_contains_no_secret(self):
        log = audit_log.AuditLog(self.path)
        log.append({"event": "X", "access_token": "ya29.FAKE-secretvalue"})
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("secretvalue", text)

    def test_disabled_log_writes_nothing(self):
        log = audit_log.AuditLog(self.path, enabled=False)
        self.assertFalse(log.append({"event": "X"}))
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(list(log.read()), [])

    def test_write_error_is_reported_not_raised(self):
        log = audit_log.AuditLog(self.path)
        with patch("builtins.open", side_effect=OSError("disk full")):
            with self.assertLogs(audit_log.logger, level="ERROR"):
                self.assertFalse(log.append({"event": "X"}))

    def test_corrupt_lines_are_skipped(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"event": "A"}\nnot json\n\n{"event": "B"}\n')
        events = [e["event"] for e in audit_log.AuditLog(self.path).read()]
        self.assertEqual(events, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
