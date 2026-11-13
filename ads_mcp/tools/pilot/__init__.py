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

"""ads-pilot-mcp tool namespaces.

``ads_mcp.coordinator`` imports every module and package under
``ads_mcp.tools`` and mounts each ``FastMCP`` instance it finds. Exposing our
sub-servers here registers them without changing the upstream coordinator.

Importing this package also installs the safety hook into
``propose_and_confirm``, so every mutate tool (upstream ones included) runs
through read-only mode, the allow-list, the limits and the audit log.
"""

from ads_mcp.safety import hooks as _hooks
from ads_mcp.reports import reports_mcp  # noqa: F401
from ads_mcp.tools.pilot.safety_tools import safety_mcp  # noqa: F401

_hooks.install()
