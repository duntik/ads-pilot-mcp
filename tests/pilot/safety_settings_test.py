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

"""Tests for ads_mcp.safety.settings."""

import os
import tempfile
import unittest
from unittest.mock import patch

from ads_mcp.safety import settings as s


class TestSafetySettings(unittest.TestCase):

    def test_defaults(self):
        cfg = s.load_settings(section={}, env={})
        self.assertFalse(cfg.read_only)
        self.assertEqual(cfg.allowed_customer_ids, ())
        self.assertEqual(cfg.limits.max_operations_per_mutate, 100)
        self.assertEqual(cfg.limits.max_budget_increase_pct, 50.0)
        self.assertIsNone(cfg.limits.max_daily_budget)
        self.assertEqual(cfg.limits.max_bid_change_pct, 50.0)
        self.assertIsNone(cfg.limits.max_cpc)
        self.assertTrue(cfg.audit_log_enabled)
        self.assertTrue(cfg.audit_log_path.endswith("audit.jsonl"))
        self.assertTrue(cfg.is_customer_allowed("1234567890"))

    def test_bundled_yaml_is_valid_and_matches_defaults(self):
        """The shipped tools_config.yaml safety section loads cleanly."""
        bundled = os.path.join(
            os.path.dirname(s.__file__), os.pardir, "tools_config.yaml"
        )
        cfg = s.load_settings(config_path=os.path.abspath(bundled), env={})
        defaults = s.load_settings(section={}, env={})
        self.assertEqual(cfg.limits, defaults.limits)
        self.assertEqual(cfg.read_only, defaults.read_only)
        self.assertEqual(cfg.allowed_customer_ids, ())

    def test_yaml_section(self):
        cfg = s.load_settings(
            section={
                "read_only": True,
                "allowed_customer_ids": ["123-456-7890", 1112223333],
                "max_operations_per_mutate": 10,
                "max_budget_increase_pct": 20,
                "max_daily_budget": 150.5,
                "max_bid_change_pct": None,
                "max_cpc": "4.25",
                "audit_log": False,
            },
            env={},
        )
        self.assertTrue(cfg.read_only)
        self.assertEqual(cfg.allowed_customer_ids, ("1234567890", "1112223333"))
        self.assertEqual(cfg.limits.max_operations_per_mutate, 10)
        self.assertEqual(cfg.limits.max_budget_increase_pct, 20.0)
        self.assertEqual(cfg.limits.max_daily_budget, 150.5)
        self.assertIsNone(cfg.limits.max_bid_change_pct)
        self.assertEqual(cfg.limits.max_cpc, 4.25)
        self.assertFalse(cfg.audit_log_enabled)
        self.assertTrue(cfg.is_customer_allowed("123-456-7890"))
        self.assertFalse(cfg.is_customer_allowed("9999999999"))

    def test_env_overrides_yaml(self):
        cfg = s.load_settings(
            section={"read_only": False, "max_cpc": 1},
            env={
                s.ENV_READ_ONLY: "true",
                s.ENV_ALLOWED_CUSTOMER_IDS: "1234567890, 111-222-3333",
                s.ENV_MAX_CPC: "none",
                s.ENV_MAX_OPERATIONS: "5",
                s.ENV_MAX_DAILY_BUDGET: "75",
                s.ENV_AUDIT_LOG: "off",
            },
        )
        self.assertTrue(cfg.read_only)
        self.assertEqual(cfg.allowed_customer_ids, ("1234567890", "1112223333"))
        self.assertIsNone(cfg.limits.max_cpc)
        self.assertEqual(cfg.limits.max_operations_per_mutate, 5)
        self.assertEqual(cfg.limits.max_daily_budget, 75.0)
        self.assertFalse(cfg.audit_log_enabled)

    def test_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = s.load_settings(section={"state_dir": tmp}, env={})
            self.assertEqual(cfg.state_dir, os.path.abspath(tmp))
            self.assertEqual(
                cfg.audit_log_path, os.path.join(cfg.state_dir, "audit.jsonl")
            )
            self.assertEqual(cfg.undo_dir, os.path.join(cfg.state_dir, "undo"))
            log = os.path.join(tmp, "custom.jsonl")
            cfg = s.load_settings(
                section={"state_dir": tmp},
                env={s.ENV_AUDIT_LOG_PATH: log},
            )
            self.assertEqual(cfg.audit_log_path, os.path.abspath(log))

    def test_customer_overrides(self):
        cfg = s.load_settings(
            section={
                "max_cpc": 2,
                "customer_overrides": {
                    "111-222-3333": {"max_cpc": 50, "max_daily_budget": 9000}
                },
            },
            env={},
        )
        self.assertEqual(cfg.limits_for("1112223333").max_cpc, 50.0)
        self.assertEqual(cfg.limits_for("1112223333").max_daily_budget, 9000)
        # Unspecified limits inherit the global values.
        self.assertEqual(cfg.limits_for("1112223333").max_bid_change_pct, 50.0)
        self.assertEqual(cfg.limits_for("1234567890").max_cpc, 2.0)

    def test_invalid_values_raise(self):
        bad_sections = [
            {"max_cpc_bid": 1},  # typo must not silently disable the limit
            {"read_only": "maybe"},
            {"max_cpc": -1},
            {"max_cpc": "lots"},
            {"max_operations_per_mutate": 2.5},
            {"max_cpc": True},
            {"allowed_customer_ids": ["abc"]},
            {"allowed_customer_ids": 5},
            {"customer_overrides": {"1234567890": {"bogus": 1}}},
            {"customer_overrides": []},
        ]
        for section in bad_sections:
            with self.subTest(section=section):
                with self.assertRaises(ValueError):
                    s.load_settings(section=section, env={})

    def test_invalid_env_raises(self):
        with self.assertRaises(ValueError):
            s.load_settings(section={}, env={s.ENV_READ_ONLY: "perhaps"})

    def test_public_dict_has_no_secret_fields(self):
        cfg = s.load_settings(section={}, env={})
        data = cfg.to_public_dict()
        self.assertIn("limits", data)
        text = repr(data).lower()
        for word in ("token", "secret", "password"):
            self.assertNotIn(word, text)

    def test_cache_and_reload(self):
        previous = s._cached
        try:
            custom = s.load_settings(section={"read_only": True}, env={})
            s.set_settings(custom)
            self.assertIs(s.get_settings(), custom)
            with patch.object(s, "load_settings", return_value="fresh"):
                self.assertEqual(s.reload_settings(), "fresh")
        finally:
            s.set_settings(previous)

    def test_default_state_dir_per_platform(self):
        with patch.object(s.sys, "platform", "linux"):
            with patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg"}):
                self.assertEqual(
                    s.default_state_dir(), os.path.join("/xdg", "ads-pilot-mcp")
                )
        with patch.object(s.sys, "platform", "win32"):
            with patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Local"}):
                self.assertEqual(
                    s.default_state_dir(),
                    os.path.join("C:\\Local", "ads-pilot-mcp"),
                )
        with patch.object(s.sys, "platform", "darwin"):
            self.assertIn("Application Support", s.default_state_dir())


if __name__ == "__main__":
    unittest.main()
