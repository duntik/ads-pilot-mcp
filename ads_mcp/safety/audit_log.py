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

"""Append-only JSONL audit log of mutations.

One JSON object per line. Entries are only ever appended, never rewritten.
Values are redacted (anything that looks like a credential is replaced) and
truncated, so the log never carries secrets or large binary payloads.
"""

from collections.abc import Iterator, Mapping
import datetime
import json
import logging
import os
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Event types.
COMMITTED = "COMMITTED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"

_MAX_STRING = 300
_REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|authorization|credential|api[_-]?key|"
    r"refresh|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
# Google OAuth access tokens (ya29.), refresh tokens (1//), JWTs, and PEM keys.
_SECRET_VALUE = re.compile(
    r"(ya29\.[\w\-.]+|1//[\w\-]{20,}|eyJ[\w\-]+\.[\w\-]+\.[\w\-]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)

_write_lock = threading.Lock()


def utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def redact(value: Any, _key: str = "") -> Any:
    """Recursively redacts secrets and truncates long strings."""
    if _key and _SECRET_KEY.search(_key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value[:100]]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        value = _SECRET_VALUE.sub(_REDACTED, value)
        if len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + "...(truncated)"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class AuditLog:
    """Writes and reads the append-only JSONL audit file."""

    def __init__(self, path: str, enabled: bool = True):
        self.path = path
        self.enabled = enabled

    def append(self, entry: Mapping[str, Any]) -> bool:
        """Appends one entry. Returns False (and logs) if writing failed.

        A failure to write the audit log never hides the API result from the
        caller, but it is logged as an error.
        """
        if not self.enabled:
            return False
        record = {"ts": utc_now_iso(), **redact(dict(entry))}
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, mode=0o700, exist_ok=True)
            with _write_lock:
                # "a" mode: the OS appends every write at end of file.
                with open(self.path, "a", encoding="utf-8") as file:
                    file.write(line)
                    file.flush()
            return True
        except OSError as exc:
            logger.error("Could not write audit log %s: %s", self.path, exc)
            return False

    def read(self) -> Iterator[dict[str, Any]]:
        """Yields entries in file order, skipping corrupt lines."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt audit log line.")
