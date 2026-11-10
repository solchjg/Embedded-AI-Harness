"""Host tier for §9.1: the MCP WiFi tools send the keys the portal reads.

The MCP server is a thin proxy, so a tool whose property name differs from the
key the handler reads sends a value the bench silently drops. `wifi_ap_start`
once sent `password` where `/api/wifi/ap_start` reads `pass`: the AP came up
open and DUTs with a stored password refused it (ESP-IDF reason 210).
"""
import importlib.util
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PORTAL = (ROOT / "pi" / "portal.py").read_text()


def _mcp():
    spec = importlib.util.spec_from_file_location("testbench_mcp", ROOT / "mcp" / "testbench_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _body_keys(path):
    """The JSON-body keys the portal handler for `path` reads."""
    route = re.search(r'path == "%s":\s*\n\s*self\.(_handle\w+)\(' % re.escape(path), PORTAL)
    assert route, "no route for " + path
    handler = re.search(r"def %s\(self[^)]*\):\n(.*?)(?=\n    def )" % route.group(1), PORTAL, re.S)
    assert handler, "no handler " + route.group(1)
    return set(re.findall(r'body(?:\.get\(|\[)\s*"(\w+)"', handler.group(1)))


@pytest.mark.parametrize("tool", ["wifi_ap_start", "wifi_sta_join"])
def test_wifi_tool_props_are_read_by_portal(tool):
    spec = _mcp().SPEC_BY_NAME[tool]
    unread = set(spec["props"]) - _body_keys(spec["path"])
    assert not unread, "%s sends keys %s that %s never reads" % (tool, sorted(unread), spec["path"])


def test_ap_start_sends_password_as_pass():
    props = _mcp().SPEC_BY_NAME["wifi_ap_start"]["props"]
    assert "pass" in props and "password" not in props
