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

"""ads-pilot-mcp safety layer.

The safety layer sits between every mutate tool and the Google Ads API:

* ``settings``  - read-only switch and policy limits (yaml + env vars).
* ``policy``    - pure functions that check a proposed change against limits.
* ``audit_log`` - append-only JSONL record of committed / blocked mutations.
* ``undo``      - "before" snapshots and reverse-operation builder.
* ``hooks``     - glue that plugs all of the above into
  ``ads_mcp.propose_and_confirm`` without rewriting it.
* ``read_only`` - hides mutate tools when read-only mode is on.

See ``docs/SAFETY.md`` for the user-facing description.
"""
