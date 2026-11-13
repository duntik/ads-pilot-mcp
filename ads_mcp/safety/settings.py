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

"""Safety settings: read-only switch, policy limits, audit and undo storage.

Settings come from the ``safety:`` section of ``tools_config.yaml`` (the same
file, resolved the same way, as the upstream namespace configuration) and can
be overridden with ``ADS_PILOT_*`` environment variables. Environment
variables win over the yaml file.

Unknown keys and invalid values raise ``ValueError`` so that a typo in a
safety limit stops the server instead of silently disabling the limit.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
import os
import sys
import threading
from typing import Any

import yaml

from ads_mcp import utils

# Environment variables (all optional).
ENV_READ_ONLY = "ADS_PILOT_READ_ONLY"
ENV_ALLOWED_CUSTOMER_IDS = "ADS_PILOT_ALLOWED_CUSTOMER_IDS"
ENV_MAX_OPERATIONS = "ADS_PILOT_MAX_OPERATIONS"
ENV_MAX_BUDGET_INCREASE_PCT = "ADS_PILOT_MAX_BUDGET_INCREASE_PCT"
ENV_MAX_DAILY_BUDGET = "ADS_PILOT_MAX_DAILY_BUDGET"
ENV_MAX_BID_CHANGE_PCT = "ADS_PILOT_MAX_BID_CHANGE_PCT"
ENV_MAX_CPC = "ADS_PILOT_MAX_CPC"
ENV_AUDIT_LOG = "ADS_PILOT_AUDIT_LOG"
ENV_AUDIT_LOG_PATH = "ADS_PILOT_AUDIT_LOG_PATH"
ENV_STATE_DIR = "ADS_PILOT_STATE_DIR"

APP_DIR_NAME = "ads-pilot-mcp"

_LIMIT_KEYS = (
    "max_operations_per_mutate",
    "max_budget_increase_pct",
    "max_daily_budget",
    "max_bid_change_pct",
    "max_cpc",
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "read_only",
        "allowed_customer_ids",
        "audit_log",
        "audit_log_path",
        "state_dir",
        "customer_overrides",
        *_LIMIT_KEYS,
    }
)
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_NONE = frozenset({"", "none", "null", "off", "unlimited"})


@dataclass(frozen=True)
class PolicyLimits:
    """Numeric limits applied to every mutation.

    Money limits are in the account's own currency units (not micros).
    ``None`` means "no limit".
    """

    max_operations_per_mutate: int | None = 100
    max_budget_increase_pct: float | None = 50.0
    max_daily_budget: float | None = None
    max_bid_change_pct: float | None = 50.0
    max_cpc: float | None = None


@dataclass(frozen=True)
class SafetySettings:
    """Effective safety configuration for the running server."""

    read_only: bool = False
    allowed_customer_ids: tuple[str, ...] = ()
    limits: PolicyLimits = field(default_factory=PolicyLimits)
    customer_overrides: Mapping[str, PolicyLimits] = field(default_factory=dict)
    audit_log_enabled: bool = True
    audit_log_path: str = ""
    state_dir: str = ""
    source: str = "defaults"

    def limits_for(self, customer_id: str) -> PolicyLimits:
        """Returns the limits for ``customer_id`` (override or global)."""
        return self.customer_overrides.get(
            utils.clean_customer_id(customer_id), self.limits
        )

    def is_customer_allowed(self, customer_id: str) -> bool:
        """An empty allow-list allows every customer the user can access."""
        if not self.allowed_customer_ids:
            return True
        return utils.clean_customer_id(customer_id) in self.allowed_customer_ids

    @property
    def undo_dir(self) -> str:
        return os.path.join(self.state_dir, "undo")

    def to_public_dict(self) -> dict[str, Any]:
        """A JSON-friendly view of the settings (contains no secrets)."""
        return {
            "read_only": self.read_only,
            "allowed_customer_ids": list(self.allowed_customer_ids),
            "limits": asdict(self.limits),
            "customer_overrides": {
                cid: asdict(lim) for cid, lim in self.customer_overrides.items()
            },
            "audit_log_enabled": self.audit_log_enabled,
            "audit_log_path": self.audit_log_path,
            "undo_dir": self.undo_dir,
            "source": self.source,
            "money_units": "account currency units (not micros)",
        }


def default_state_dir() -> str:
    """Returns the per-user data directory for audit and undo files."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            os.path.join("~", "AppData", "Local")
        )
    elif sys.platform == "darwin":
        base = os.path.expanduser(
            os.path.join("~", "Library", "Application Support")
        )
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
            os.path.join("~", ".local", "share")
        )
    return os.path.join(base, APP_DIR_NAME)


def _parse_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(
        f"Safety setting '{name}' must be true or false, got {value!r}."
    )


def _parse_number(name: str, value: Any, *, integer: bool = False) -> Any:
    """Parses a non-negative number; None-like values mean "no limit"."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(
            f"Safety setting '{name}' must be a number, got {value!r}."
        )
    if isinstance(value, str):
        if value.strip().lower() in _NONE:
            return None
        try:
            value = float(value.strip())
        except ValueError as exc:
            raise ValueError(
                f"Safety setting '{name}' must be a number, got {value!r}."
            ) from exc
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"Safety setting '{name}' must be a number, got {value!r}."
        )
    if value < 0:
        raise ValueError(f"Safety setting '{name}' must not be negative.")
    if integer:
        if float(value) != int(value):
            raise ValueError(f"Safety setting '{name}' must be a whole number.")
        return int(value)
    return float(value)


def _parse_customer_ids(name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [v for v in value.replace(";", ",").split(",") if v.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(
            f"Safety setting '{name}' must be a list of customer IDs."
        )
    cleaned = []
    for item in items:
        cid = utils.clean_customer_id(item)
        if not cid:
            raise ValueError(
                f"Safety setting '{name}' contains an invalid customer ID: {item!r}."
            )
        cleaned.append(cid)
    return tuple(dict.fromkeys(cleaned))


def _parse_limits(
    raw: Mapping[str, Any], base: PolicyLimits, where: str
) -> PolicyLimits:
    updates: dict[str, Any] = {}
    for key in _LIMIT_KEYS:
        if key in raw:
            updates[key] = _parse_number(
                f"{where}{key}",
                raw[key],
                integer=(key == "max_operations_per_mutate"),
            )
    return replace(base, **updates)


def _read_yaml_section(config_path: str | None) -> tuple[dict[str, Any], str]:
    # Imported lazily: config imports nothing from safety, but keeping the
    # dependency one-directional makes upstream merges simpler.
    from ads_mcp.config import ToolsConfig

    resolved = ToolsConfig._resolve_config_path(config_path)
    if resolved is None:
        return {}, "defaults"
    with open(resolved, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Configuration root in '{resolved}' must be a mapping."
        )
    section = data.get("safety") or {}
    if not isinstance(section, dict):
        raise ValueError(
            f"The 'safety' section in '{resolved}' must be a mapping."
        )
    return section, resolved


def load_settings(
    config_path: str | None = None,
    env: Mapping[str, str] | None = None,
    section: Mapping[str, Any] | None = None,
) -> SafetySettings:
    """Builds ``SafetySettings`` from yaml (or ``section``) plus environment.

    Args:
        config_path: Optional explicit yaml path (otherwise resolved like the
            upstream tools configuration).
        env: Environment mapping, defaults to ``os.environ``.
        section: Use this dict as the yaml ``safety`` section instead of
            reading a file (mainly for tests).
    """
    env = os.environ if env is None else env
    if section is None:
        raw, source = _read_yaml_section(config_path)
    else:
        raw, source = dict(section), "inline"

    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(
            "Unknown safety setting(s): "
            + ", ".join(sorted(unknown))
            + ". Valid keys: "
            + ", ".join(sorted(_TOP_LEVEL_KEYS))
            + "."
        )

    read_only = _parse_bool("read_only", raw.get("read_only", False))
    allowed = _parse_customer_ids(
        "allowed_customer_ids", raw.get("allowed_customer_ids")
    )
    limits = _parse_limits(raw, PolicyLimits(), "")
    audit_enabled = _parse_bool("audit_log", raw.get("audit_log", True))
    audit_path = raw.get("audit_log_path") or None
    state_dir = raw.get("state_dir") or None

    overrides_raw = raw.get("customer_overrides")
    if overrides_raw is None:
        overrides_raw = {}
    if not isinstance(overrides_raw, dict):
        raise ValueError(
            "Safety setting 'customer_overrides' must be a mapping."
        )

    # Environment overrides.
    if env.get(ENV_READ_ONLY) is not None:
        read_only = _parse_bool(ENV_READ_ONLY, env[ENV_READ_ONLY])
    if env.get(ENV_ALLOWED_CUSTOMER_IDS) is not None:
        allowed = _parse_customer_ids(
            ENV_ALLOWED_CUSTOMER_IDS, env[ENV_ALLOWED_CUSTOMER_IDS]
        )
    env_limits = {
        "max_operations_per_mutate": env.get(ENV_MAX_OPERATIONS),
        "max_budget_increase_pct": env.get(ENV_MAX_BUDGET_INCREASE_PCT),
        "max_daily_budget": env.get(ENV_MAX_DAILY_BUDGET),
        "max_bid_change_pct": env.get(ENV_MAX_BID_CHANGE_PCT),
        "max_cpc": env.get(ENV_MAX_CPC),
    }
    limits = _parse_limits(
        {k: v for k, v in env_limits.items() if v is not None}, limits, "env:"
    )
    if env.get(ENV_AUDIT_LOG) is not None:
        audit_enabled = _parse_bool(ENV_AUDIT_LOG, env[ENV_AUDIT_LOG])
    if env.get(ENV_AUDIT_LOG_PATH):
        audit_path = env[ENV_AUDIT_LOG_PATH]
    if env.get(ENV_STATE_DIR):
        state_dir = env[ENV_STATE_DIR]

    # Per-customer overrides start from the (already env-adjusted) globals.
    overrides: dict[str, PolicyLimits] = {}
    for cid_raw, override in overrides_raw.items():
        cid = utils.clean_customer_id(cid_raw)
        if not cid or not isinstance(override, dict):
            raise ValueError(
                f"Invalid customer_overrides entry for {cid_raw!r}: expected "
                "a customer ID mapped to limit settings."
            )
        bad = set(override) - set(_LIMIT_KEYS)
        if bad:
            raise ValueError(
                f"Unknown limit(s) in customer_overrides[{cid_raw!r}]: "
                + ", ".join(sorted(bad))
            )
        overrides[cid] = _parse_limits(
            override, limits, f"customer_overrides[{cid}]."
        )

    state_dir = os.path.abspath(
        os.path.expanduser(str(state_dir or default_state_dir()))
    )
    audit_path = os.path.abspath(
        os.path.expanduser(
            str(audit_path or os.path.join(state_dir, "audit.jsonl"))
        )
    )

    return SafetySettings(
        read_only=read_only,
        allowed_customer_ids=allowed,
        limits=limits,
        customer_overrides=overrides,
        audit_log_enabled=audit_enabled,
        audit_log_path=audit_path,
        state_dir=state_dir,
        source=source,
    )


_lock = threading.Lock()
_cached: SafetySettings | None = None


def get_settings() -> SafetySettings:
    """Returns the cached settings, loading them on first use."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = load_settings()
        return _cached


def reload_settings() -> SafetySettings:
    """Drops the cache and loads settings again."""
    global _cached
    with _lock:
        _cached = None
    return get_settings()


def set_settings(settings: SafetySettings | None) -> None:
    """Replaces the cached settings (``None`` forces a reload on next use)."""
    global _cached
    with _lock:
        _cached = settings
