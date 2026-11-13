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

"""Test doubles shared by the ads-pilot-mcp tests."""

from collections.abc import Callable, Sequence
import os
import shutil
import tempfile
from typing import Any
from unittest.mock import MagicMock

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.types.google_ads_service import (
    GoogleAdsRow,
)
from google.auth.credentials import AnonymousCredentials

from ads_mcp.safety import settings as settings_mod

CUSTOMER_ID = "1234567890"

_real_client: GoogleAdsClient | None = None


def real_client() -> GoogleAdsClient:
    """A GoogleAdsClient used only to build request/type objects offline."""
    global _real_client
    if _real_client is None:
        _real_client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
        )
    return _real_client


def row(**kwargs: Any) -> GoogleAdsRow:
    return GoogleAdsRow(**kwargs)


class FakeBatch:
    def __init__(self, results: Sequence[Any]):
        self.results = list(results)
        self.field_mask = MagicMock()


class FakeAdsService:
    """Answers ``search_stream`` by matching a substring of the query.

    ``responders`` is a list of ``(substring, rows)``; the first matching
    substring wins. Every query is recorded in ``queries``.
    """

    def __init__(
        self,
        responders: Sequence[tuple[str, Sequence[Any] | Callable[[str], Any]]],
        currency: str = "USD",
        time_zone: str = "America/New_York",
    ):
        self.responders = list(responders)
        self.queries: list[str] = []
        self.account_row = row(
            customer={
                "descriptive_name": "Test Account",
                "currency_code": currency,
                "time_zone": time_zone,
            }
        )
        self.mutate = MagicMock()

    def search_stream(self, customer_id: str, query: str):
        self.queries.append(query)
        if "customer.currency_code" in query:
            return [FakeBatch([self.account_row])]
        for key, rows in self.responders:
            if key in query:
                result = rows(query) if callable(rows) else rows
                return [FakeBatch(result)]
        return [FakeBatch([])]

    def query_matching(self, substring: str) -> str:
        for q in self.queries:
            if substring in q:
                return q
        raise AssertionError(
            f"No query containing {substring!r}: {self.queries}"
        )


class TempStateMixin:
    """Gives each test its own state dir and settings; restores afterwards."""

    def make_settings(self, **section: Any) -> settings_mod.SafetySettings:
        section.setdefault("state_dir", self.state_dir)
        return settings_mod.load_settings(section=section, env={})

    def setUp(self) -> None:  # noqa: D401 - unittest hook
        super().setUp()
        self.state_dir = tempfile.mkdtemp(prefix="ads-pilot-unit-")
        self._previous_settings = settings_mod._cached
        settings_mod.set_settings(self.make_settings())

    def tearDown(self) -> None:
        settings_mod.set_settings(self._previous_settings)
        shutil.rmtree(self.state_dir, ignore_errors=True)
        super().tearDown()

    def use_settings(self, **section: Any) -> settings_mod.SafetySettings:
        s = self.make_settings(**section)
        settings_mod.set_settings(s)
        return s

    @property
    def audit_path(self) -> str:
        return os.path.join(self.state_dir, "audit.jsonl")
