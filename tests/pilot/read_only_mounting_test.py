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

"""Read-only mode hides every mutate tool when the server mounts tools."""

import importlib
import pkgutil
import unittest
from unittest.mock import patch

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ads_mcp.tools as tools_pkg
from ads_mcp.config import ToolsConfig
from ads_mcp.coordinator import initialize_and_mount_tools
from ads_mcp.safety import read_only

from . import fakes

ALL_NAMESPACES = {
    "namespaces": {
        "customers": True,
        "search": True,
        "metadata": True,
        "labels": True,
        "assets": True,
        "reports": True,
        "safety": True,
    }
}


def _sub_servers():
    servers = []
    for _, name, _ in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"ads_mcp.tools.{name}")
        for value in vars(module).values():
            if isinstance(value, FastMCP) and value not in servers:
                servers.append(value)
    return servers


class TestReadOnlyMounting(
    fakes.TempStateMixin, unittest.IsolatedAsyncioTestCase
):

    def setUp(self):
        super().setUp()
        # Mounting removes tools from the shared sub-servers; restore them
        # afterwards so other tests still see every tool.
        self._saved = {
            id(s): (s, dict(s.local_provider._components))
            for s in _sub_servers()
        }

    def tearDown(self):
        for server, components in self._saved.values():
            server.local_provider._components.clear()
            server.local_provider._components.update(components)
        super().tearDown()

    async def mounted_tools(self):
        parent = FastMCP("Test Parent")
        with patch(
            "ads_mcp.config.ToolsConfig.load",
            return_value=ToolsConfig(ALL_NAMESPACES),
        ):
            initialize_and_mount_tools(parent)
        return {t.name: t for t in await parent.list_tools()}

    async def test_read_only_hides_mutate_tools(self):
        self.use_settings(read_only=True)
        tools = await self.mounted_tools()
        for name, tool in tools.items():
            self.assertTrue(
                tool.annotations and tool.annotations.readOnlyHint,
                f"{name} is visible in read-only mode",
            )
        for hidden in [
            "labels_manage_labels",
            "labels_manage_label_associations",
            "assets_create_asset",
            "assets_upload_video_asset",
            "safety_undo_change",
        ]:
            self.assertNotIn(hidden, tools)
        for visible in [
            "search_search",
            "customers_list_accessible_customers",
            "reports_account_overview",
            "safety_get_safety_settings",
            "safety_list_changes",
        ]:
            self.assertIn(visible, tools)

    async def test_normal_mode_keeps_mutate_tools(self):
        tools = await self.mounted_tools()
        self.assertIn("labels_manage_labels", tools)
        self.assertIn("safety_undo_change", tools)
        self.assertIn("reports_wasted_spend", tools)

    def test_unannotated_tools_count_as_mutate(self):
        class Tool:
            annotations = None

        self.assertFalse(read_only.is_read_only_tool(Tool()))
        Tool.annotations = ToolAnnotations(readOnlyHint=False)
        self.assertFalse(read_only.is_read_only_tool(Tool()))
        Tool.annotations = ToolAnnotations(readOnlyHint=True)
        self.assertTrue(read_only.is_read_only_tool(Tool()))
        self.use_settings(read_only=True)
        Tool.annotations = None
        self.assertTrue(read_only.should_hide(Tool()))
        self.use_settings(read_only=False)
        self.assertFalse(read_only.should_hide(Tool()))


if __name__ == "__main__":
    unittest.main()
