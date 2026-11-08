"""USB port paths and slot prefixes: pure logic shared by the portal and
rfc2217-learn-slots. No sysfs, no udev, no I/O, so the host tier can test it.

A udev ID_PATH ends in ``usb-<bus>:<ports>:<config>.<interface>``, for example
``platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1.3:1.0``. The *port path*
is ``<bus>:<ports>`` (``0:1.1.3``): the chain of hub ports from the root hub to
the device, one number per hub. A slot's ``usb_prefix`` is a port path.

A prefix owns its own port and every port below it (a board with an onboard
hub, or an external hub plugged into that port), but only whole hops count:
``0:1.1`` owns ``0:1.1`` and ``0:1.1.3``, never ``0:1.10`` or ``0:1.12``. The
longest prefix that owns a device wins, so ``0:1.1.3`` beats ``0:1.1``.
"""

import re

_PORT_PATH_RE = re.compile(r"^\d+:\d+(?:\.\d+)*$")


def port_path(id_path: str) -> str | None:
    """'...-usb-0:1.1.3:1.0' -> '0:1.1.3'; None when there is no usb- part.

    The last 'usb-' is the one before the port path: the controller name
    ('platform-3f980000.usb') can contain 'usb' too.
    """
    if not id_path:
        return None
    idx = id_path.rfind("usb-")
    if idx < 0:
        return None
    parts = id_path[idx + 4:].split(":")
    if len(parts) < 2:
        return None
    candidate = f"{parts[0]}:{parts[1]}"
    return candidate if _PORT_PATH_RE.match(candidate) else None


def is_port_path(prefix: str | None) -> bool:
    return bool(prefix) and bool(_PORT_PATH_RE.match(prefix))


def prefix_owns(prefix: str, slot_key: str) -> bool:
    """True when a device with *slot_key* belongs to the port *prefix*.

    A prefix that is not a port path (a hand-written fragment of an ID_PATH)
    keeps the old substring match, so an existing config still loads.
    """
    if not prefix or not slot_key:
        return False
    if not is_port_path(prefix):
        return prefix in slot_key
    path = port_path(slot_key)
    if path is None:
        return False
    return path_owns(prefix, path)


def path_owns(prefix: str, path: str) -> bool:
    """True when port path *path* is *prefix* or lies below it."""
    return path == prefix or path.startswith(prefix + ".")


def best_prefix(prefixes, slot_key: str) -> str | None:
    """The longest prefix in *prefixes* that owns *slot_key*, or None."""
    owners = [p for p in prefixes if prefix_owns(p, slot_key)]
    if not owners:
        return None
    return max(owners, key=lambda p: (p.count("."), len(p)))


def hops_below(prefix: str, slot_key: str) -> int:
    """How many hub hops the device sits below *prefix* (0 = on that port).

    More than 0 means an external hub (or a board's onboard hub) sits on the
    slot's port. If it is an external hub, every board on it lands in this one
    slot: give each hub port its own usb_prefix.
    """
    path = port_path(slot_key)
    if not is_port_path(prefix) or path is None:
        return 0
    if path == prefix or not path.startswith(prefix + "."):
        return 0
    return path.count(".") - prefix.count(".")


def top_level(path: str) -> str:
    """'0:1.3.4' -> '0:1.3': the host-side port, whatever hubs follow it."""
    bus, _, ports = path.partition(":")
    return f"{bus}:{'.'.join(ports.split('.')[:2])}"


def sysfs_name(prefix: str) -> str | None:
    """Port path '0:1.1.3' -> sysfs USB device name '1-1.1.3' (bus is 1-based)."""
    if not is_port_path(prefix):
        return None
    bus, _, ports = prefix.partition(":")
    return f"{int(bus) + 1}-{ports}"


def sysfs_name_in(prefix: str, name: str) -> bool:
    """True when sysfs device *name* ('1-1.1.3') is on or below *prefix*."""
    base = sysfs_name(prefix)
    return base is not None and (name == base or name.startswith(base + "."))


def sort_key(prefix: str):
    """Order port paths numerically: 0:1.1.2 before 0:1.1.10."""
    if not is_port_path(prefix):
        return (1, prefix)
    bus, _, ports = prefix.partition(":")
    return (0, [int(bus)] + [int(p) for p in ports.split(".")])


# ── Merging learned ports into an existing testbench.json ────────────────

def _label_num(label: str) -> int | None:
    m = re.fullmatch(r"SLOT(\d+)", label or "")
    return int(m.group(1)) if m else None


def merge_config(existing: dict | None, learned: list[dict],
                 pins: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """Fold freshly learned ports into an existing testbench.json.

    *learned* is one {"usb_prefix", "serial"} per occupied port. *pins* maps a
    slot label to a USB serial (an ESP32-S3's is its MAC): that label moves to
    whichever port the board with that serial is on now.

    Kept from *existing*: gpio pins, debug probes, every slot's label and TCP,
    GDB and telnet ports, and slots whose port is still valid (an empty port
    stays a slot). Dropped: a slot whose prefix now has a hub on it, i.e. a
    learned port lies below it, since it would take every board on that hub.
    New ports get the next free SLOTn and the ports that number implies, or
    the next unused ones.

    Returns (config, notes): notes say, in words, what changed.
    """
    existing = existing or {}
    pins = {k: v.lower() for k, v in (pins or {}).items()}
    notes: list[str] = []
    cfg = {k: v for k, v in existing.items() if k != "slots"}
    cfg.setdefault("gpio_boot", 18)
    cfg.setdefault("gpio_en", 17)
    cfg.setdefault("debug_probes", [])

    prefixes = [entry["usb_prefix"] for entry in learned]
    serial_at = {entry["usb_prefix"]: (entry.get("serial") or "").lower()
                 for entry in learned}

    slots_out: list[dict] = []
    for s in existing.get("slots", []):
        s = dict(s)
        p = s.get("usb_prefix")
        below = [q for q in prefixes
                 if is_port_path(p) and q != p and path_owns(p, q)]
        if below:
            notes.append(f"{s['label']}: dropped {p}, a hub sits there now "
                         f"(ports {', '.join(sorted(below, key=sort_key))})")
            s["usb_prefix"] = None
        slots_out.append(s)

    for label, serial in pins.items():
        port = next((p for p in prefixes if serial_at.get(p) == serial), None)
        if port is None:
            notes.append(f"{label}: pinned board {serial} not found, left as is")
            continue
        for s in slots_out:
            if s.get("usb_prefix") == port and s["label"] != label:
                notes.append(f"{s['label']}: gave {port} to {label}")
                s["usb_prefix"] = None
        target = next((s for s in slots_out if s["label"] == label), None)
        if target is None:
            target = {"label": label}
            slots_out.append(target)
        if target.get("usb_prefix") != port:
            notes.append(f"{label}: now {port} (board {serial})")
        target["usb_prefix"] = port

    # A slot that lost its port and is not pinned is reused for a new port
    # before a new label is made, so SLOT numbers stay compact.
    taken = {s.get("usb_prefix") for s in slots_out}
    free = [s for s in slots_out if not s.get("usb_prefix")]
    for p in sorted(prefixes, key=sort_key):
        if p in taken:
            continue
        if free:
            s = free.pop(0)
            notes.append(f"{s['label']}: now {p}")
        else:
            used = {_label_num(s["label"]) for s in slots_out}
            n = 1
            while n in used:
                n += 1
            s = {"label": f"SLOT{n}"}
            slots_out.append(s)
            notes.append(f"{s['label']}: new, {p}")
        s["usb_prefix"] = p
        taken.add(p)
    for s in free:
        notes.append(f"{s['label']}: removed, no port left for it")
    slots_out = [s for s in slots_out if s.get("usb_prefix")]

    for key, base in (("tcp_port", 4000), ("gdb_port", 3332),
                      ("openocd_telnet_port", 4443)):
        used = {s[key] for s in slots_out if s.get(key)}
        for s in slots_out:
            if s.get(key):
                continue
            n = _label_num(s["label"]) or 1
            port = base + n
            while port in used:
                port += 1
            s[key] = port
            used.add(port)

    slots_out.sort(key=lambda s: (_label_num(s["label"]) is None,
                                  _label_num(s["label"]) or 0, s["label"]))
    order = ("label", "usb_prefix", "tcp_port", "gdb_port", "openocd_telnet_port")
    cfg["slots"] = [{k: s[k] for k in order if k in s}
                    | {k: v for k, v in s.items() if k not in order}
                    for s in slots_out]
    return cfg, notes


# ── Which of a slot's serial devices is the board's console (FR-046) ─────
#
# A board can put more than one serial device on its slot: a cellular board's
# modem (SIMCom, Quectel) adds four or more ttyACM ports beside the board's own
# USB-UART bridge, and they come and go whenever the firmware powers the modem.
# The proxy belongs on the console. Modem ports must neither take the proxy
# nor count as the board flapping.

# Vendors whose serial device is a board's console: USB-UART bridges (WCH,
# Silicon Labs, FTDI, Prolific) and Espressif's native USB.
CONSOLE_VIDS = frozenset({"1a86", "10c4", "0403", "067b", "303a"})
# Vendors whose serial devices are a cellular modem's ports, never a console.
MODEM_VIDS = frozenset({"1e0e", "2c7c", "1bc7", "05c6"})

_USB_ID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")


def clean_usb_id(value) -> str | None:
    """'1A86:55D3' -> '1a86:55d3'; None for anything that is not VID:PID."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if _USB_ID_RE.match(v) else None


def device_role(usb_id: str | None, console_id: str | None = None) -> str:
    """'console', 'aux' or 'other' for a serial device with *usb_id*.

    With a slot's ``console_usb_id`` set, only that VID:PID is the console and
    every other device is aux. Without it, the vendor decides: a known bridge
    or Espressif is the console, a known modem is aux, anything else (unknown,
    or a Notecard) is 'other', which takes the proxy only when no console is
    there.
    """
    usb_id = clean_usb_id(usb_id)
    console_id = clean_usb_id(console_id)
    if console_id:
        if usb_id is None:
            return "other"
        return "console" if usb_id == console_id else "aux"
    if usb_id is None:
        return "other"
    vid = usb_id.split(":")[0]
    if vid in CONSOLE_VIDS:
        return "console"
    if vid in MODEM_VIDS:
        return "aux"
    return "other"


def console_keys(ids: dict, console_id: str | None = None) -> list:
    """The keys of *ids* (key -> VID:PID or None) that may carry the proxy.

    Consoles first; when there is none, the 'other' devices. Aux devices
    never. In the order of *ids*.
    """
    roles = {k: device_role(v, console_id) for k, v in ids.items()}
    first = [k for k, r in roles.items() if r == "console"]
    return first or [k for k, r in roles.items() if r == "other"]


def is_aux(ids: dict, key, console_id: str | None = None) -> bool:
    """True when device *key* of a slot (*ids*: key -> VID:PID or None) is
    not its console: a modem port, anything but ``console_usb_id`` when that
    is set, or an unrecognised device while a console sits beside it. Such a
    device is listed on the slot but never takes the proxy and never counts
    as the board flapping."""
    role = device_role(ids.get(key), console_id)
    if role == "aux":
        return True
    if role == "console":
        return False
    return any(device_role(v, console_id) == "console"
               for k, v in ids.items() if k != key)
