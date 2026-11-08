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
- FR-045: a slot can pin its console to one USB VID:PID, so other serial
  devices on the same port path (a board's modem, say) are neither the
  console nor a reason to restart it.
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

_VID_PID = re.compile(r"^([0-9a-f]{4}):([0-9a-f]{4})$")
_USB_DEVICE = re.compile(r"^\d+-\d+(?:\.\d+)*$")


def clean_console_id(value) -> str | None:
    """FR-045: a slot's `console_vid_pid` from testbench.json, normalised.

    "1A86:55D3" -> "1a86:55d3"; None or "" -> None (no pin). Anything else is
    refused with ValueError, so a typo is logged rather than silently pinning
    the console to nothing.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"console_vid_pid must be a string, got {value!r}")
    v = value.strip().lower()
    if not v:
        return None
    if not _VID_PID.match(v):
        raise ValueError(f"console_vid_pid {value!r} is not VID:PID "
                         f"(four hex digits each, e.g. 1a86:55d3)")
    return v


def usb_device_from_devpath(devpath: str) -> str | None:
    """'/devices/.../1-1.1.1.4/1-1.1.1.4:1.0/tty/ttyACM0' -> '1-1.1.1.4'.

    The USB device is the last path element that is a sysfs device name, not
    an interface ('1-1.1.1.4:1.0'), a root hub ('usb1') or the tty itself.
    """
    for part in reversed((devpath or "").split("/")):
        if _USB_DEVICE.match(part):
            return part
    return None


def is_other_device(console_id: str | None, vid_pid: str | None) -> bool:
    """FR-045: is a serial device with *vid_pid* on a pinned slot not its console?

    True only when the slot is pinned and the device's VID:PID is known and
    differs. An unpinned slot, or a device whose VID:PID could not be read,
    keeps the old behaviour (every serial device on the port is the slot's),
    so the pin can only narrow what the slot follows, never lose its console
    to a failed sysfs read.
    """
    if not console_id or not vid_pid:
        return False
    return vid_pid.lower() != console_id
