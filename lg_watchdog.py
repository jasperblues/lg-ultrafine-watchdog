#!/usr/bin/python3
"""LG UltraFine 5K flapping watchdog for the ASUS ProArt PX13.

Background: on this laptop the LG drops its Thunderbolt link when charging stops,
and can get stuck dropping/reconnecting every few seconds (e.g. when it is
plugged in while the battery is above the charge limit). Letting the laptop
charge again (charge limit off) stops the flapping immediately.

What this does:
  * follows the kernel log for "thunderbolt N-2: device disconnected"
  * on >= FLAP_DROPS drops within FLAP_WINDOW seconds, turns the battery charge
    limit off via UPower (no root needed) and shows a notification
  * when the LG has been gone for UNDOCK_GRACE seconds (undocked), turns the
    charge limit back on -- but only if this watchdog turned it off and nobody
    re-enabled it in the meantime (disable with RESTORE_LIMIT_ON_UNDOCK = False)
  * while docked, holds a systemd-logind "handle-lid-switch" inhibitor so a
    display drop with the lid closed can't send the laptop to sleep
"""
import json
import logging
import os
import re
import select
import subprocess
import time

from gi.repository import Gio, GLib

FLAP_DROPS = 3
FLAP_WINDOW = 30        # seconds
COOLDOWN = 300          # min seconds between interventions
UNDOCK_GRACE = 60       # LG absent this long => undocked
RESTORE_LIMIT_ON_UNDOCK = os.environ.get("LG_WATCHDOG_RESTORE_LIMIT", "1") == "1"
STATE = os.path.expanduser("~/.local/state/lg-watchdog.json")
BATTERY = "/org/freedesktop/UPower/devices/battery_BAT0"

DISCONNECT = re.compile(r"thunderbolt \d+-2: device disconnected")
CONNECT = re.compile(r"thunderbolt \d+-2: LG Electronics UltraFine")

log = logging.getLogger("lg-watchdog")


class Battery:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def _call(self, iface, method, args):
        return self.bus.call_sync("org.freedesktop.UPower", BATTERY, iface, method,
                                  args, None, Gio.DBusCallFlags.NONE, 5000, None)

    def limit_enabled(self):
        r = self._call("org.freedesktop.DBus.Properties", "Get",
                       GLib.Variant("(ss)", ("org.freedesktop.UPower.Device", "ChargeThresholdEnabled")))
        return bool(r.unpack()[0])

    def set_limit(self, enabled):
        self._call("org.freedesktop.UPower.Device", "EnableChargeThreshold",
                   GLib.Variant("(b)", (enabled,)))


def lg_connected_now():
    """True if an LG UltraFine 5K is currently enumerated on Thunderbolt."""
    base = "/sys/bus/thunderbolt/devices"
    for dev in os.listdir(base):
        try:
            with open(f"{base}/{dev}/vendor") as v, open(f"{base}/{dev}/device") as d:
                if v.read().strip() == "0x1e" and d.read().strip() == "0x1114":
                    return True
        except OSError:
            continue
    return False


class LidInhibitor:
    """Hold a logind handle-lid-switch block while docked."""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.fd = None

    def hold(self):
        if self.fd is not None:
            return
        try:
            res, fds = self.bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                "Inhibit", GLib.Variant("(ssss)", ("handle-lid-switch", "LG watchdog",
                                                   "LG display docked; don't suspend if it blinks", "block")),
                GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, 5000, None, None)
            self.fd = fds.get(res.unpack()[0])
            log.info("docked: holding lid-switch inhibitor")
        except GLib.Error as e:
            log.warning("could not take lid inhibitor: %s", e.message)

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            log.info("undocked: released lid-switch inhibitor")


def notify(summary, body):
    subprocess.run(["notify-send", "--app-name=LG watchdog", "--icon=video-display", summary, body],
                   check=False)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"we_disabled_limit": False}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    battery = Battery()
    lid = LidInhibitor()
    state = load_state()
    drops = []
    last_action = 0.0
    lg_present = lg_connected_now()
    lg_gone_since = None if lg_present else time.monotonic()
    if lg_present:
        lid.hold()

    follow = subprocess.Popen(["journalctl", "-k", "-f", "-n", "0", "-o", "cat"],
                              stdout=subprocess.PIPE, text=True)
    log.info("watching kernel log (limit currently %s)", "on" if battery.limit_enabled() else "off")

    while True:
        now = time.monotonic()
        ready, _, _ = select.select([follow.stdout], [], [], 1.0)
        line = follow.stdout.readline() if ready else ""
        if ready and not line:
            raise SystemExit("journalctl exited")
        if line:
            if DISCONNECT.search(line):
                drops.append(now)
                lg_present = False
                lg_gone_since = now
                log.info("LG disconnect")
            elif CONNECT.search(line):
                lg_present = True
                lg_gone_since = None
                lid.hold()

        drops = [t for t in drops if now - t <= FLAP_WINDOW]

        # Flapping: let the laptop charge so the LG settles.
        if len(drops) >= FLAP_DROPS and now - last_action > COOLDOWN:
            last_action = now
            drops.clear()
            try:
                if battery.limit_enabled():
                    battery.set_limit(False)
                    state["we_disabled_limit"] = True
                    save_state(state)
                    log.info("flapping: charge limit turned OFF")
                    notify("LG display was flapping",
                           "Turned the battery charge limit off so the laptop charges and the "
                           "display settles. It will be turned back on when you undock.")
                else:
                    log.info("flapping, but the charge limit is already off")
                    notify("LG display is flapping — plug in the laptop charger",
                           "The charge limit is already off, so letting the laptop charge "
                           "can't settle it (battery full). Plugging in the laptop's own charger "
                           "stops it instantly; otherwise unplug and replug the display.")
            except GLib.Error as e:
                log.warning("could not change charge limit: %s", e.message)

        undocked = not lg_present and lg_gone_since is not None and now - lg_gone_since >= UNDOCK_GRACE
        if undocked:
            lid.release()

        # Undocked: restore the charge limit if we were the ones who turned it off.
        if RESTORE_LIMIT_ON_UNDOCK and state.get("we_disabled_limit") and undocked:
            try:
                if not battery.limit_enabled():
                    battery.set_limit(True)
                    log.info("undocked: charge limit turned back ON")
            except GLib.Error as e:
                log.warning("could not change charge limit: %s", e.message)
            state["we_disabled_limit"] = False
            save_state(state)
            lg_gone_since = None


if __name__ == "__main__":
    main()
