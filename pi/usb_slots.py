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
