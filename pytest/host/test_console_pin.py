"""Host tier for FR-045 in the portal itself: what a hotplug event does to a
slot pinned to its console's VID:PID.

The case: a Waveshare ESP32-S3-SIM7670G in SLOT2. Its CH343 (1a86:55d3) is the
console; when the firmware powers the modem, the SIM7670G's four CDC ports
appear on the same hub path. Before the pin they became the slot's devnodes,
counted toward flapping and restarted its proxy.

portal.py needs gpiod, which only the Pi has; a stub is enough because no test
here touches a GPIO. sysfs and the proxy are replaced by fakes.
"""
import sys
import types

import pytest

sys.modules.setdefault("gpiod", types.ModuleType("gpiod"))
sys.modules.setdefault("gpiod.line", types.ModuleType("gpiod.line"))
portal = pytest.importorskip("portal")

CH343 = "1a86:55d3"
SIM7670 = "1e0e:9011"
BASE = "platform-3f980000.usb-usb-0:1.1.1"
CONSOLE_KEY = BASE + ".4:1.0"
MODEM_KEYS = [BASE + f".2:1.{i}" for i in range(2, 6)]
UNKNOWN_KEY = BASE + ".3:1.0"   # a device whose VID:PID cannot be read
OTHER_KEY = BASE + ".1:1.0"     # an ESP32's native USB


@pytest.fixture
def bench(monkeypatch, tmp_path):
    cfg = tmp_path / "testbench.json"
    cfg.write_text('{"slots": [{"label": "SLOT2", "usb_prefix": "0:1.1.1", '
                   '"tcp_port": 4002, "console_vid_pid": "1A86:55D3"}]}')
    monkeypatch.setattr(portal, "slots", portal.load_config(str(cfg)))
    ids = {CONSOLE_KEY: CH343, UNKNOWN_KEY: None, OTHER_KEY: "303a:1001",
           **{k: SIM7670 for k in MODEM_KEYS}}
    by_node = {}
    monkeypatch.setattr(portal, "_usb_vid_pid",
                        lambda devnode, devpath: by_node.get(devnode))
    calls = []
    def start(s):
        calls.append(("start", s["devnode"]))
        s.update(running=True, pid=1)

    def stop(s):
        calls.append(("stop", s["devnode"]))
        s.update(running=False, pid=None)
    monkeypatch.setattr(portal, "start_proxy", start)
    monkeypatch.setattr(portal, "stop_proxy", stop)
    monkeypatch.setattr(portal, "_refresh_all_usb_devices", lambda: None)
    monkeypatch.setattr(portal, "log_activity", lambda *a, **k: None)
    monkeypatch.setattr(portal, "_start_flap_recovery",
                        lambda s: calls.append(("recover", s["label"])))

    class Thread:   # run the proxy start inline, so the test sees it
        def __init__(self, target, daemon=None, **kw):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(portal.threading, "Thread", Thread)
    monkeypatch.setattr(portal.time, "sleep", lambda s: None)

    def event(action, key, node):
        if action == "add":
            by_node[node] = ids[key]
        h = portal.Handler.__new__(portal.Handler)
        h._read_json = lambda: {"action": action, "devnode": node, "id_path": key}
        out = {}
        h._send_json = lambda data, status=200: out.update(data)
        h._handle_hotplug()
        return out

    return portal.slots["_fixed_SLOT2"], event, calls


def test_fr045_pin_read_from_config(bench):
    slot, _, _ = bench
    assert slot["_console_id"] == CH343
    assert portal._slot_info(slot)["console_vid_pid"] == CH343


def test_fr045_modem_ports_change_nothing(bench):
    slot, event, calls = bench
    event("add", CONSOLE_KEY, "/dev/ttyACM0")
    assert slot["devnode"] == "/dev/ttyACM0" and calls == [("start", "/dev/ttyACM0")]
    calls.clear()
    events_before = list(slot["_event_times"])
    # The modem powers up, and off again, three times: 24 events, more than
    # FLAP_THRESHOLD, none of which may reach the slot.
    for _ in range(3):
        for n, key in enumerate(MODEM_KEYS, start=1):
            assert event("add", key, f"/dev/ttyACM{n}")["other_device"] is True
        info = portal._slot_info(slot)
        assert sorted(info["other_devnodes"]) == [f"/dev/ttyACM{n}" for n in range(1, 5)]
        for n, key in enumerate(MODEM_KEYS, start=1):
            assert event("remove", key, f"/dev/ttyACM{n}")["other_device"] is True
    assert calls == []
    assert slot["devnode"] == "/dev/ttyACM0" and slot["present"]
    assert list(slot["_devnodes"].values()) == ["/dev/ttyACM0"]
    assert slot["_event_times"] == events_before and not slot["flapping"]
    assert portal._slot_info(slot)["other_devnodes"] == []


def test_fr045_modem_first_never_becomes_the_console(bench):
    slot, event, calls = bench
    event("add", MODEM_KEYS[0], "/dev/ttyACM0")
    assert slot["devnode"] is None and not slot["present"] and calls == []
    event("add", CONSOLE_KEY, "/dev/ttyACM4")
    assert slot["devnode"] == "/dev/ttyACM4"
    assert calls == [("start", "/dev/ttyACM4")]


def test_fr045_pinned_console_takes_the_proxy_from_an_unknown_device(bench):
    slot, event, calls = bench
    event("add", UNKNOWN_KEY, "/dev/ttyACM0")   # kept: its id is unreadable
    assert slot["devnode"] == "/dev/ttyACM0"
    calls.clear()
    event("add", CONSOLE_KEY, "/dev/ttyACM1")
    assert slot["devnode"] == "/dev/ttyACM1"
    assert calls == [("stop", "/dev/ttyACM1"), ("start", "/dev/ttyACM1")]


def test_fr045_unpinned_slot_sets_modems_aside_by_vendor(bench):
    slot, event, calls = bench
    slot["_console_id"] = None
    assert event("add", MODEM_KEYS[0], "/dev/ttyACM1")["other_device"] is True
    assert slot["devnode"] is None and calls == []
    event("add", OTHER_KEY, "/dev/ttyACM2")
    assert slot["devnode"] == "/dev/ttyACM2" and calls == [("start", "/dev/ttyACM2")]
