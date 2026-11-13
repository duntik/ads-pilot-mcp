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

"""Tests for the ads-pilot-mcp additions.

Test discovery imports this package before any test runs. It points the
safety layer's state directory (audit log, undo snapshots) at a temporary
directory so that no test run, including the upstream tests that commit
mocked mutations, writes into the real per-user data directory.
"""

import os
import tempfile

if not os.environ.get("ADS_PILOT_STATE_DIR"):
    os.environ["ADS_PILOT_STATE_DIR"] = tempfile.mkdtemp(
        prefix="ads-pilot-test-"
    )

from ads_mcp.safety import settings as _settings  # noqa: E402

_settings.set_settings(None)
