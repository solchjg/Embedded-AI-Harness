"""Host tier for FR-002 slot mapping: which slot a USB device belongs to.

Pure logic only (pi/usb_slots.py). The case that motivated it: a multi-port
hub on one Pi port gave every board the same Pi-side prefix, so the bench
could not tell slots apart.
"""
import importlib.machinery
import importlib.util
import pathlib

import pytest

import usb_slots as us

PI4 = "platform-fd500000.pcie-pci-0000:01:00.0-usb-"
PI3 = "platform-3f980000.usb-usb-"


@pytest.mark.parametrize("id_path,want", [
    (PI3 + "0:1.1:1.0", "0:1.1"),
    (PI3 + "0:1.3.4:1.0", "0:1.3.4"),
    (PI4 + "0:1.2.1.3:1.2", "0:1.2.1.3"),
    (PI4 + "1:2:1.0", "1:2"),
    ("pci-0000:01:00.0", None),
    ("", None),
])
def test_port_path_strips_the_interface(id_path, want):
    assert us.port_path(id_path) == want


def test_each_hub_port_gets_its_own_slot():
    prefixes = ["0:1.1.1", "0:1.1.2", "0:1.1.3", "0:1.1.4"]
    for p in prefixes:
        assert us.best_prefix(prefixes, PI4 + p + ":1.0") == p


def test_longest_owner_wins_over_the_pi_port():
    prefixes = ["0:1.1", "0:1.1.3"]
    assert us.best_prefix(prefixes, PI4 + "0:1.1.3:1.0") == "0:1.1.3"
    assert us.best_prefix(prefixes, PI4 + "0:1.1.2:1.0") == "0:1.1"
    assert us.hops_below("0:1.1", PI4 + "0:1.1.2:1.0") == 1
    assert us.hops_below("0:1.1.3", PI4 + "0:1.1.3:1.0") == 0


@pytest.mark.parametrize("key", ["0:1.10:1.0", "0:1.12.3:1.0", "0:11.1:1.0", "10:1.1:1.0"])
def test_a_prefix_owns_whole_hops_only(key):
    assert not us.prefix_owns("0:1.1", PI4 + key)


def test_a_non_port_prefix_keeps_substring_matching():
    assert us.prefix_owns("usb-0:1.1", PI3 + "0:1.1.2:1.0")


def test_sysfs_names_follow_whole_hops():
    assert us.sysfs_name("0:1.1.3") == "1-1.1.3"
    assert us.sysfs_name_in("0:1.1", "1-1.1.3")
    assert not us.sysfs_name_in("0:1.1", "1-1.10")


def test_sort_is_numeric():
    got = sorted(["0:1.1.10", "0:1.2", "0:1.1.2"], key=us.sort_key)
    assert got == ["0:1.1.2", "0:1.1.10", "0:1.2"]


def _learn_slots():
    path = pathlib.Path(us.__file__).with_name("rfc2217-learn-slots")
    loader = importlib.machinery.SourceFileLoader("learn_slots", str(path))
    spec = importlib.util.spec_from_loader("learn_slots", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_learn_slots_keeps_the_hub_port_by_default():
    ls = _learn_slots()
    assert ls.extract_usb_prefix(PI4 + "0:1.1.3:1.0") == "0:1.1.3"
    assert ls.extract_usb_prefix(PI4 + "0:1.1.3:1.0", top_level=True) == "0:1.1"


# ── OpenOCD must name the board, not the hub port ────────────────────────

def _dc():
    import debug_controller
    return debug_controller


JTAG = {"product": "USB JTAG/serial debug unit", "vid_pid": "303a:1001"}


def test_jtag_location_names_the_board_behind_a_hub():
    devs = [dict(JTAG, usb_path="1-1.1.3")]
    assert _dc().jtag_location(devs, "0:1.1") == "1-1.1.3"


def test_jtag_location_falls_back_to_the_prefix():
    dc = _dc()
    assert dc.jtag_location([], "0:1.2") == "1-1.2"
    assert dc.jtag_location([dict(JTAG)], "0:1.2") == "1-1.2"   # old scan, no path
    two = [dict(JTAG, usb_path="1-1.1.2"), dict(JTAG, usb_path="1-1.1.3")]
    assert dc.jtag_location(two, "0:1.1") == "1-1.1"            # ambiguous
    other = [{"product": "CP2102", "vid_pid": "10c4:ea60", "usb_path": "1-1.1.2"}]
    assert dc.jtag_location(other, "0:1.1") == "1-1.1"


# ── merge_config: re-learning after a hub change keeps what the bench knew ─

OLD = {
    "gpio_boot": 18, "gpio_en": 17,
    "slots": [
        {"label": "SLOT1", "usb_prefix": "0:1.1", "tcp_port": 4001, "gdb_port": 3333, "openocd_telnet_port": 4444},
        {"label": "SLOT2", "usb_prefix": "0:1.3", "tcp_port": 4002, "gdb_port": 3334, "openocd_telnet_port": 4445},
    ],
    "debug_probes": [{"label": "PROBE1", "bus_port": "1-1.4:1.0"}],
}
TDISPLAY = "48:27:E2:EA:48:3C"
HUB = [
    {"usb_prefix": "0:1.1.1", "serial": ""},
    {"usb_prefix": "0:1.1.2", "serial": TDISPLAY},
    {"usb_prefix": "0:1.1.3", "serial": "AA:BB"},
    {"usb_prefix": "0:1.3", "serial": "CC:DD"},
]


def _by_label(cfg):
    return {s["label"]: s for s in cfg["slots"]}


def test_merge_pins_a_board_and_keeps_its_ports():
    cfg, notes = us.merge_config(OLD, HUB, {"SLOT1": TDISPLAY.lower()})
    got = _by_label(cfg)
    assert got["SLOT1"]["usb_prefix"] == "0:1.1.2"
    assert (got["SLOT1"]["tcp_port"], got["SLOT1"]["gdb_port"]) == (4001, 3333)
    assert got["SLOT2"]["usb_prefix"] == "0:1.3"
    assert {got[k]["usb_prefix"] for k in ("SLOT3", "SLOT4")} == {"0:1.1.1", "0:1.1.3"}
    assert cfg["debug_probes"] == OLD["debug_probes"] and cfg["gpio_en"] == 17
    assert any("hub sits there" in n for n in notes)


def test_merge_never_keeps_a_prefix_that_now_holds_a_hub():
    cfg, _ = us.merge_config(OLD, HUB)
    assert "0:1.1" not in {s["usb_prefix"] for s in cfg["slots"]}


def test_merge_gives_unique_ports_and_labels():
    cfg, _ = us.merge_config(OLD, HUB, {"SLOT1": TDISPLAY})
    for key in ("label", "usb_prefix", "tcp_port", "gdb_port", "openocd_telnet_port"):
        vals = [s[key] for s in cfg["slots"]]
        assert len(vals) == len(set(vals)), key


def test_merge_is_stable_when_nothing_moved():
    cfg, _ = us.merge_config(OLD, HUB, {"SLOT1": TDISPLAY})
    again, notes = us.merge_config(cfg, HUB, {"SLOT1": TDISPLAY})
    assert again == cfg and notes == []


def test_merge_without_a_config_numbers_ports_in_order():
    cfg, _ = us.merge_config(None, HUB)
    assert [s["usb_prefix"] for s in cfg["slots"]] == ["0:1.1.1", "0:1.1.2", "0:1.1.3", "0:1.3"]
    assert [s["tcp_port"] for s in cfg["slots"]] == [4001, 4002, 4003, 4004]


def test_merge_reports_a_pinned_board_that_is_missing():
    _, notes = us.merge_config(OLD, HUB, {"SLOT1": "00:00"})
    assert any("not found" in n for n in notes)
