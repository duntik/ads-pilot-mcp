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

"""Read-only mode: decide which tools to hide.

A tool counts as read-only only when it is explicitly annotated with
``readOnlyHint=True``. Anything else (including tools with no annotations)
is treated as a mutate tool and hidden in read-only mode. The safety hook in
``propose_and_confirm`` also refuses every mutation in read-only mode, so a
tool that slipped through would still be blocked.
"""

from typing import Any

from ads_mcp.safety import settings as settings_mod


def is_read_only_tool(tool: Any) -> bool:
    annotations = getattr(tool, "annotations", None)
    return bool(annotations is not None and annotations.readOnlyHint is True)


def should_hide(tool: Any) -> bool:
    """True when read-only mode is on and ``tool`` may change the account."""
    return settings_mod.get_settings().read_only and not is_read_only_tool(tool)
