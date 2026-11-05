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
