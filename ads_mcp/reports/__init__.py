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

"""Curated, read-only report tools (namespace ``reports``).

Each tool builds its GAQL from constants plus validated parameters (see
``gaql.py``), converts money from micros into the account currency, pages
and caps its rows, and returns compact JSON with a short markdown summary.
"""

from fastmcp import FastMCP

reports_mcp = FastMCP("reports")

# Importing the modules registers their tools on ``reports_mcp``.
from ads_mcp.reports import (  # noqa: E402,F401
    account,
    budget,
    performance,
    search_terms,
)
