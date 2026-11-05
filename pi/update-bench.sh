#!/usr/bin/env bash
# update-bench.sh — update the testbench software and re-map its slots.
#
# Run on the Pi, from a clone of this repository:
#   sudo bash pi/update-bench.sh                         # pull, install, re-learn slots
#   sudo bash pi/update-bench.sh --pin SLOT1=48:27:E2:EA:48:3C
#   sudo bash pi/update-bench.sh --no-learn              # code only, keep testbench.json
# Other options: --no-pull (install the checkout as it is), --occupied-only (no
# slots for empty hub ports), --yes (write the config without asking), --force
# (go ahead although a slot is in a debug session).
#
# Steps, each shown before it runs:
#   1. Refuse while a slot is in a debug session (the restart would cut it),
#      unless --force.
#   2. git pull --ff-only (as the clone's owner, not root), unless --no-pull.
#   3. install.sh --update: scripts, udev rules, portal restart.
#   4. rfc2217-learn-slots --merge: fold the ports that now hold boards (and
#      the empty ports on the same hubs) into the existing testbench.json,
#      keeping labels, TCP/GDB ports, GPIO pins and debug probes. --pin keeps
#      a label on a given board wherever it is plugged. Shows the diff, asks,
#      backs up the old file as testbench.json.<time>.bak, writes the new one.
#   5. Restarts the portal and prints every slot: port, device, chip.
#
# Use it after changing hubs or cables, or after merging harness changes.
set -euo pipefail

CONFIG=/etc/rfc2217/testbench.json
API=http://127.0.0.1:8080/api/devices
PULL=1 LEARN=1 YES=0 FORCE=0 EMPTY=1
PINS=()

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --pin)       PINS+=(--pin "$2"); shift ;;
        --pin=*)     PINS+=(--pin "${1#--pin=}") ;;
        --no-pull)   PULL=0 ;;
        --no-learn)  LEARN=0 ;;
        --occupied-only) EMPTY=0 ;;
        --yes|-y)    YES=1 ;;
        --force)     FORCE=1 ;;
        -h|--help)   usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

PI_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$PI_DIR/.." && pwd)"
OWNER="$(stat -c %U "$REPO")"

step() { printf '\n== %s\n' "$*"; }

confirm() {
    [ "$YES" = 1 ] && return 0
    read -r -p "$1 [y/N] " a
    [ "$a" = y ] || [ "$a" = Y ]
}

slot_table() {
    curl -s -m 5 "$API" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit("portal not answering")
row = "{:8} {:8} {:14} {:10} {}"
print(row.format("slot", "present", "devnode", "chip", "usb"))
for s in d.get("slots", []):
    usb = ", ".join("%s %s" % (u.get("usb_path", "?"), u.get("product", ""))
                    for u in s.get("usb_devices") or [])
    print(row.format(s.get("label", "?"), str(s.get("present")),
                     s.get("devnode") or "-", s.get("detected_chip") or "-",
                     usb or "-"))
'
}

wait_portal() {
    for _ in $(seq 1 30); do
        curl -s -m 2 -o /dev/null "$API" && return 0
        sleep 1
    done
    echo "portal did not answer within 30 s: journalctl -u rfc2217-portal -n 50" >&2
    return 1
}

# 1. Nothing may be mid-debug-session: the restart would cut it.
step "Checking for slots in use"
busy=$(curl -s -m 5 "$API" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
print(" ".join(s["label"] for s in d.get("slots", []) if s.get("debugging")))
' || true)
if [ -n "$busy" ]; then
    echo "In a debug session: $busy"
    if [ "$FORCE" != 1 ]; then
        echo "Restarting the portal would end it. Re-run when it is done, or pass --force." >&2
        exit 1
    fi
fi
echo "${busy:-none}"

# 2. Code
if [ "$PULL" = 1 ]; then
    step "Pulling $(sudo -u "$OWNER" git -C "$REPO" remote get-url origin)"
    sudo -u "$OWNER" git -C "$REPO" pull --ff-only
    sudo -u "$OWNER" git -C "$REPO" log --oneline -1
fi

# 3. Install
step "Installing scripts (install.sh --update)"
(cd "$PI_DIR" && bash install.sh --update)
wait_portal

# 4. Slots
if [ "$LEARN" = 1 ]; then
    step "Learning slots"
    learn_args=(--merge "$CONFIG" --json "${PINS[@]}")
    [ "$EMPTY" = 1 ] && learn_args+=(--include-empty)
    new=$(mktemp)
    trap 'rm -f "$new"' EXIT
    rfc2217-learn-slots "${learn_args[@]}" >"$new"
    if [ -f "$CONFIG" ] && cmp -s <(python3 -m json.tool "$CONFIG") <(python3 -m json.tool "$new"); then
        echo "testbench.json already matches the ports: unchanged"
    else
        if [ -f "$CONFIG" ]; then
            diff -u "$CONFIG" "$new" || true
        else
            cat "$new"
        fi
        if confirm "Write this to $CONFIG?"; then
            if [ -f "$CONFIG" ]; then
                bak="$CONFIG.$(date +%Y%m%d-%H%M%S).bak"
                cp -p "$CONFIG" "$bak"
                echo "old config kept as $bak"
            fi
            mkdir -p "$(dirname "$CONFIG")"
            install -m 644 "$new" "$CONFIG"
            step "Restarting the portal"
            systemctl restart rfc2217-portal
            wait_portal
        else
            echo "not written"
        fi
    fi
fi

# 5. Result. Chip detection runs in the background after a restart, so give
# it a moment before reporting.
step "Slots"
sleep 5
slot_table
