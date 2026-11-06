"""Per-slot facts an operator sets or the bench measured, judged for display.

Pure: no files, no hardware, no clock of its own. portal.py reads and writes
the JSON files and passes the records in.

- FR-041: a slot carries an operator-set board name ("T-SIM7080G"), so several
  boards on one bench are told apart by more than their slot number.
- FR-043: BOOT/EN pins mean something only on an ESP32, so a board the
  bench can tell is something else (a Notecard, say) is not offered the test.
- FR-042: a BOOT/EN wiring measurement belongs to one board at one time. It is
  shown only while that board is in the slot, and is not trusted once another
  board is there or the measurement is older than its maximum age.
- FR-044: each slot can have its own BOOT/EN pair, so two boards can be wired
  at once without a reset of one reaching the other.
- FR-045: a native-USB ESP32 shows an EN reset by leaving the USB bus, not by
  a ROM banner the bench could read, so the wiring test counts either.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

# The bench's operator works in US Eastern time; every time the dashboard
# shows is in that zone, with EST/EDT spelled out.
BENCH_TZ = ZoneInfo("America/New_York")

BOARD_NAME_MAX = 40
WIRING_MAX_AGE_S_DEFAULT = 7 * 24 * 3600

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_board_name(name) -> str | None:
    """An operator's board name, tidied, or None to clear it.

    Raises ValueError for a value that is not a string or is too long.
    """
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    name = " ".join(_CONTROL.sub(" ", name).split())
    if not name:
        return None
    if len(name) > BOARD_NAME_MAX:
        raise ValueError(f"name is longer than {BOARD_NAME_MAX} characters")
    return name


def bench_time(ts: float | None) -> str | None:
    """Epoch seconds as bench-local text, e.g. "2026-11-06 08:33 EST"."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, BENCH_TZ).strftime("%Y-%m-%d %H:%M %Z")


def bench_iso(ts: float) -> str:
    """Epoch seconds as ISO 8601 with the Eastern offset, for stored records."""
    return datetime.fromtimestamp(ts, BENCH_TZ).isoformat(timespec="seconds")


# USB vendors behind which an ESP32's BOOT/EN can sit: Espressif's own USB
# (native USB-Serial/JTAG) and the USB-UART bridges ESP32 boards carry.
ESP_USB_VIDS = frozenset({
    "303a",   # Espressif
    "10c4",   # Silicon Labs CP210x
    "1a86",   # WCH CH340/CH343
    "0403",   # FTDI
    "067b",   # Prolific PL2303
})


def boot_pins_apply(detected_chip: str | None, usb_devices: list[dict]) -> bool:
    """FR-043: could the board in this slot have ESP32 BOOT/EN pins?

    True for a detected ESP chip, or a USB device from Espressif or a
    USB-UART bridge. False only when the slot's USB devices are known and
    none of them is either, so an unrecognised slot keeps the test.
    """
    if detected_chip and detected_chip.lower().startswith("esp"):
        return True
    if not usb_devices:
        return True
    return any((d.get("vid_pid") or "").lower().split(":")[0] in ESP_USB_VIDS
               for d in usb_devices)


def wiring_verdict(record: dict | None, present: bool,
                   board_serial: str | None, now: float,
                   max_age_s: float = WIRING_MAX_AGE_S_DEFAULT) -> dict:
    """Judge a stored BOOT/EN measurement against the board in the slot now.

    Returns:
      status  "untested" | "wired" | "not_wired" | "stale" | "other_board"
      wired   True/False only for a current measurement of this board, else
              None — recovery takes the GPIO path only on True
      show    whether the dashboard shows it: only while a board is present,
              and never a result that belongs to another board
      checked_at  when it was measured, in bench time (None if unknown)
      reason  why a result is stale, else None
    """
    out = {"status": "untested", "wired": None, "show": False,
           "checked_at": None, "reason": None}
    if not record:
        return out
    ts = record.get("checked_ts")
    out["checked_at"] = bench_time(ts) if ts is not None else record.get("checked_at")

    recorded_serial = record.get("board_serial")
    if (board_serial and recorded_serial
            and board_serial != recorded_serial):
        out["status"] = "other_board"
        return out

    if "board_serial" not in record or ts is None:
        # Written before measurements named their board and time: nothing
        # says which board it was, so it cannot be trusted for this one.
        out["status"] = "stale"
        out["reason"] = "recorded before the bench noted which board it tested"
    elif now - ts > max_age_s:
        out["status"] = "stale"
        days = max_age_s / 86400
        out["reason"] = (f"older than {days:g} day{'s' if days != 1 else ''}"
                         if days >= 1 else
                         f"older than {max_age_s / 3600:g} h")
    else:
        wired = bool(record.get("gpio_wired"))
        out["status"] = "wired" if wired else "not_wired"
        out["wired"] = wired
    out["show"] = bool(present)
    return out


# FR-044 ───────────────────────────────────────────────────────────────────

_PIN_KEYS = ("gpio_boot", "gpio_en")


def slot_gpio_pins(bench: dict, slot_defs: list[dict],
                   allowed: set[int] | frozenset[int]) -> tuple[dict, dict, list[str]]:
    """FR-044: the BOOT/EN pins each slot drives, from testbench.json.

    *bench* holds the bench-wide `gpio_boot`/`gpio_en`; each entry of
    *slot_defs* may name its own. Rules:

    - A slot that names a key uses it; `null` there means no wire.
    - A slot that names neither key inherits the bench-wide pin, unless some
      slot names that pin for itself: a pin wired to one board is never also
      driven for another slot.
    - A pin outside *allowed*, not an integer, BOOT and EN the same pin, or a
      pin another slot already named (first in file order wins) is dropped
      for that slot with a note, leaving that line unwired.

    Returns (pins, default, notes): pins maps label → {"gpio_boot",
    "gpio_en"}; default is the pair a slot not in the file (one the bench
    makes for an unknown port) gets; notes say, in words, what was dropped.
    """
    notes: list[str] = []

    def valid(label, key, pin):
        if pin is None:
            return None
        if isinstance(pin, bool) or not isinstance(pin, int) or pin not in allowed:
            notes.append(f"{label}: {key} {pin!r} is not an allowed Pi GPIO "
                         f"({min(allowed)}-{max(allowed)}); left unwired")
            return None
        return pin

    bench_pins = {k: valid("bench", k, bench.get(k)) for k in _PIN_KEYS}

    # Pins slots name for themselves, first claim wins.
    owner: dict[int, str] = {}
    pins: dict[str, dict] = {}
    for sdef in slot_defs:
        label = sdef["label"]
        if not any(k in sdef for k in _PIN_KEYS):
            continue
        own = {k: valid(label, k, sdef.get(k)) for k in _PIN_KEYS}
        if own["gpio_boot"] is not None and own["gpio_boot"] == own["gpio_en"]:
            notes.append(f"{label}: BOOT and EN are both GPIO "
                         f"{own['gpio_boot']}; left unwired")
            own = {k: None for k in _PIN_KEYS}
        for k in _PIN_KEYS:
            pin = own[k]
            if pin is None:
                continue
            if pin in owner:
                notes.append(f"{label}: {k} GPIO {pin} is already "
                             f"{owner[pin]}'s; left unwired")
                own[k] = None
            else:
                owner[pin] = label
        pins[label] = own

    default = {k: (None if bench_pins[k] in owner else bench_pins[k])
               for k in _PIN_KEYS}
    for k in _PIN_KEYS:
        if bench_pins[k] is not None and default[k] is None:
            notes.append(f"bench-wide {k} GPIO {bench_pins[k]} belongs to "
                         f"{owner[bench_pins[k]]}; other slots do not drive it")
    for sdef in slot_defs:
        pins.setdefault(sdef["label"], dict(default))
    return pins, default, notes


# FR-045 ───────────────────────────────────────────────────────────────────

ROM_BANNER = re.compile(r"ESP-ROM:|rst:0x[0-9a-fA-F]+|boot:0x[0-9a-fA-F]+")


# A native-USB board drops off the bus within milliseconds of EN going low; a
# removal later than this is the board's own doing (deep sleep, a crash), not
# the pulse's.
USB_RESET_WINDOW_S = 1.5


def reset_evidence(since: float, serial_lines, hotplug_events,
                   usb_window_s: float = USB_RESET_WINDOW_S) -> str | None:
    """FR-045: did the board in a slot reset after *since*, and how is it known?

    *serial_lines* are {"ts", "text"} entries from the slot's serial buffer;
    *hotplug_events* are (ts, action) pairs for the slot. Returns "banner"
    for a ROM boot banner on serial (a board behind a USB-UART bridge),
    "usb" for the board leaving the USB bus within *usb_window_s* of *since*
    (a native-USB ESP32, whose USB-Serial/JTAG goes down with the chip and
    comes back after the ROM has printed its banner to nobody), or None. A
    later removal is not counted: a board going to deep sleep on its own must
    not pass for a wired EN.
    """
    for entry in serial_lines:
        if entry.get("ts", 0) > since and ROM_BANNER.search(entry.get("text", "")):
            return "banner"
    for ts, action in hotplug_events:
        if since < ts <= since + usb_window_s and action == "remove":
            return "usb"
    return None


def returned_after(since: float, hotplug_events) -> bool:
    """FR-045: has the board come back on USB (an add) after *since*?"""
    return any(ts > since and action == "add" for ts, action in hotplug_events)
