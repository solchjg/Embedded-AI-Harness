"""Host tier for FR-041 (board names) and FR-042 (BOOT/EN result staleness)."""
import pytest

import slot_meta as sm

MAC = "48:27:E2:EA:48:3C"
OTHER = "AA:BB:CC:DD:EE:FF"
T0 = 1_793_000_000.0          # 2026-10-26, a fixed clock
DAY = 86400.0


def rec(wired=False, serial=MAC, ts=T0):
    return {"gpio_wired": wired, "en_wired": wired, "boot_wired": wired,
            "detail": "", "board_serial": serial, "checked_ts": ts,
            "checked_at": sm.bench_iso(ts)}


# ── FR-041 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("T-SIM7080G", "T-SIM7080G"),
    ("  T-Display   AMOLED \n", "T-Display AMOLED"),
    ("", None),
    ("   ", None),
    (None, None),
    ("a\x00b", "a b"),
])
def test_fr041_clean_board_name(raw, want):
    assert sm.clean_board_name(raw) == want


@pytest.mark.parametrize("raw", ["x" * (sm.BOARD_NAME_MAX + 1), 7, ["a"]])
def test_fr041_board_name_refused(raw):
    with pytest.raises(ValueError):
        sm.clean_board_name(raw)


# ── FR-043 ────────────────────────────────────────────────────────────────

NOTECARD = [{"product": "Notecard", "vid_pid": "30a4:0001"}]
ESP_USB = [{"product": "USB JTAG/serial debug unit", "vid_pid": "303a:1001"}]
CP2102 = [{"product": "CP2102 USB to UART", "vid_pid": "10c4:ea60"}]


@pytest.mark.parametrize("chip,devs,want", [
    ("esp32s3", ESP_USB, True),
    (None, ESP_USB, True),       # native USB before the chip is probed
    (None, CP2102, True),        # bridge board: chip never auto-detected
    (None, NOTECARD, False),
    ("esp32", NOTECARD, True),   # a detected ESP chip always wins
    (None, [], True),            # nothing known: keep the test
])
def test_fr043_boot_pins_apply(chip, devs, want):
    assert sm.boot_pins_apply(chip, devs) is want


# ── FR-042 ────────────────────────────────────────────────────────────────

def test_fr042_untested():
    v = sm.wiring_verdict(None, True, MAC, T0)
    assert v["status"] == "untested" and v["wired"] is None and not v["show"]


@pytest.mark.parametrize("wired,status", [(False, "not_wired"),
                                          (True, "wired")])
def test_fr042_current_result_shown_for_its_board(wired, status):
    v = sm.wiring_verdict(rec(wired), True, MAC, T0 + 60)
    assert v["status"] == status and v["wired"] is wired and v["show"]


def test_fr042_hidden_while_slot_empty_but_kept():
    v = sm.wiring_verdict(rec(False), False, None, T0 + 60)
    assert not v["show"]
    assert v["wired"] is False   # still the fact recovery relies on mid-flap


def test_fr042_other_board_not_shown_or_trusted():
    v = sm.wiring_verdict(rec(True), True, OTHER, T0 + 60)
    assert v["status"] == "other_board"
    assert v["wired"] is None and not v["show"]


def test_fr042_old_result_is_stale():
    v = sm.wiring_verdict(rec(True), True, MAC, T0 + 8 * DAY, 7 * DAY)
    assert v["status"] == "stale" and v["wired"] is None and v["show"]
    assert "7 days" in v["reason"]


def test_fr042_legacy_record_is_stale():
    legacy = {"gpio_wired": False, "detail": "",
              "checked_at": "2026-11-01T10:00:00"}
    v = sm.wiring_verdict(legacy, True, MAC, T0)
    assert v["status"] == "stale" and v["wired"] is None
    assert v["checked_at"] == "2026-11-01T10:00:00"


def test_fr042_board_without_serial_judged_by_age_only():
    v = sm.wiring_verdict(rec(True, serial=None), True, None, T0 + 60)
    assert v["status"] == "wired" and v["wired"] is True


@pytest.mark.parametrize("ts,want", [
    (1_793_000_000.0, "EDT"),   # late October: daylight time
    (1_794_500_000.0, "EST"),   # mid November: standard time
])
def test_fr042_times_are_us_eastern(ts, want):
    assert sm.bench_time(ts).endswith(want)


# ── FR-044 ────────────────────────────────────────────────────────────────

ALLOWED = frozenset(range(16, 28))
BENCH = {"gpio_boot": 18, "gpio_en": 17}


def pins(slots, bench=BENCH):
    return sm.slot_gpio_pins(bench, slots, ALLOWED)


def test_fr044_no_slot_pins_keeps_the_bench_wide_pair():
    got, default, notes = pins([{"label": "SLOT1"}, {"label": "SLOT3"}])
    assert got["SLOT1"] == got["SLOT3"] == {"gpio_boot": 18, "gpio_en": 17}
    assert default == {"gpio_boot": 18, "gpio_en": 17}
    assert notes == []


def test_fr044_two_slots_with_their_own_pairs():
    got, default, notes = pins([
        {"label": "SLOT1", "gpio_boot": 23, "gpio_en": 22},
        {"label": "SLOT2"},
        {"label": "SLOT3", "gpio_boot": 18, "gpio_en": 17},
    ])
    assert got["SLOT1"] == {"gpio_boot": 23, "gpio_en": 22}
    assert got["SLOT3"] == {"gpio_boot": 18, "gpio_en": 17}
    # SLOT3 took the bench-wide pair, so nobody else drives it.
    assert got["SLOT2"] == {"gpio_boot": None, "gpio_en": None}
    assert default == {"gpio_boot": None, "gpio_en": None}
    assert any("belongs to SLOT3" in n for n in notes)


def test_fr044_null_means_no_wire():
    got, _, notes = pins([{"label": "SLOT4", "gpio_boot": None, "gpio_en": None},
                          {"label": "SLOT1"}])
    assert got["SLOT4"] == {"gpio_boot": None, "gpio_en": None}
    assert got["SLOT1"] == {"gpio_boot": 18, "gpio_en": 17}
    assert notes == []


def test_fr044_a_slot_naming_one_key_has_no_other():
    got, _, _ = pins([{"label": "SLOT1", "gpio_en": 22}])
    assert got["SLOT1"] == {"gpio_boot": None, "gpio_en": 22}


def test_fr044_a_pin_claimed_twice_stays_with_the_first_slot():
    got, _, notes = pins([
        {"label": "SLOT1", "gpio_boot": 23, "gpio_en": 22},
        {"label": "SLOT2", "gpio_boot": 23, "gpio_en": 24},
    ])
    assert got["SLOT1"] == {"gpio_boot": 23, "gpio_en": 22}
    assert got["SLOT2"] == {"gpio_boot": None, "gpio_en": 24}
    assert any("SLOT2" in n and "SLOT1's" in n for n in notes)


@pytest.mark.parametrize("bad", [5, 12, 40, "22", 22.0, True])
def test_fr044_a_pin_outside_the_allowed_set_is_dropped(bad):
    got, _, notes = pins([{"label": "SLOT1", "gpio_boot": bad, "gpio_en": 22}])
    assert got["SLOT1"] == {"gpio_boot": None, "gpio_en": 22}
    assert notes and "not an allowed" in notes[0]


def test_fr044_boot_and_en_on_one_pin_is_refused():
    got, _, notes = pins([{"label": "SLOT1", "gpio_boot": 22, "gpio_en": 22}])
    assert got["SLOT1"] == {"gpio_boot": None, "gpio_en": None}
    assert "both GPIO 22" in notes[0]


def test_fr044_a_bench_without_wires():
    got, default, notes = pins([{"label": "SLOT1"}],
                               bench={"gpio_boot": None, "gpio_en": None})
    assert got["SLOT1"] == default == {"gpio_boot": None, "gpio_en": None}
    assert notes == []


# ── FR-045 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("1A86:55D3", "1a86:55d3"),
    (" 1a86:55d3 ", "1a86:55d3"),
    ("", None),
    (None, None),
])
def test_fr045_clean_console_id(raw, want):
    assert sm.clean_console_id(raw) == want


@pytest.mark.parametrize("raw", ["1a86", "1a86:55d", "1a86-55d3", "zz86:55d3", 0x1a86, ["1a86:55d3"]])
def test_fr045_console_id_refused(raw):
    with pytest.raises(ValueError):
        sm.clean_console_id(raw)


@pytest.mark.parametrize("devpath,want", [
    ("/devices/platform/soc/3f980000.usb/usb1/1-1/1-1.1/1-1.1.1/1-1.1.1.4/"
     "1-1.1.1.4:1.0/tty/ttyACM0", "1-1.1.1.4"),
    ("/devices/platform/scb/fd500000.pcie/pci0000:00/0000:00:00.0/0000:01:00.0/"
     "usb1/1-1/1-1.3/1-1.3:1.2/tty/ttyACM2", "1-1.3"),
    ("/devices/virtual/tty/ttyS0", None),
    ("", None),
])
def test_fr045_usb_device_from_devpath(devpath, want):
    assert sm.usb_device_from_devpath(devpath) == want


CH343 = "1a86:55d3"
SIM7670 = "1e0e:9011"


@pytest.mark.parametrize("pin,vid_pid,other", [
    (CH343, CH343, False),       # the console
    (CH343, "1A86:55D3", False),  # case ignored
    (CH343, SIM7670, True),      # the modem: set aside
    (CH343, None, False),        # unreadable: kept, never lose the console
    (None, SIM7670, True),       # unpinned: a modem vendor is still aside
    (None, "2c7c:0125", True),   # Quectel
    (None, CH343, False),        # unpinned: anything else is the slot's
    (None, "303a:1001", False),  # Espressif native USB
    (None, None, False),
])
def test_fr045_is_other_device(pin, vid_pid, other):
    assert sm.is_other_device(pin, vid_pid) is other


@pytest.mark.parametrize("pin,vid_pid,want", [
    (CH343, CH343, True), (CH343, SIM7670, False), (CH343, None, False),
    (None, CH343, False),
])
def test_fr045_is_console(pin, vid_pid, want):
    assert sm.is_console(pin, vid_pid) is want


@pytest.mark.parametrize("vid_pid,want", [
    (CH343, False), ("10c4:ea60", False),   # bridges: open at once
    ("303a:1001", True), (None, True),      # native USB, unknown: wait
])
def test_fr045_boot_delay_only_for_native_usb(vid_pid, want):
    assert sm.needs_boot_delay(vid_pid) is want

# ── FR-046 ────────────────────────────────────────────────────────────────

def test_fr045_banner_after_the_pulse_counts():
    lines = [{"ts": T0 + 0.5, "text": "ESP-ROM:esp32s3-20210327"}]
    assert sm.reset_evidence(T0, lines, []) == "banner"


def test_fr045_banner_from_before_the_pulse_does_not():
    lines = [{"ts": T0 - 1, "text": "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)"}]
    assert sm.reset_evidence(T0, lines, []) is None


def test_fr045_native_usb_leaving_the_bus_counts():
    events = [(T0 + 0.05, "remove"), (T0 + 1.2, "add")]
    assert sm.reset_evidence(T0, [], events) == "usb"
    assert sm.returned_after(T0, events)


def test_fr045_an_add_alone_is_not_a_reset():
    assert sm.reset_evidence(T0, [], [(T0 + 0.2, "add")]) is None


def test_fr045_a_late_removal_is_the_board_sleeping_not_en():
    # A board that goes to deep sleep on its own seconds later must not pass
    # for a wired EN.
    assert sm.reset_evidence(T0, [], [(T0 + 5.0, "remove")]) is None


def test_fr045_events_before_the_pulse_are_ignored():
    events = [(T0 - 0.5, "remove"), (T0 - 0.1, "add")]
    assert sm.reset_evidence(T0, [], events) is None
    assert not sm.returned_after(T0, events)


def test_fr045_not_back_until_an_add():
    assert not sm.returned_after(T0, [(T0 + 0.05, "remove")])
