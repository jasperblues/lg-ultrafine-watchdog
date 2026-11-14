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
    re-enabled it in the meantime
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
    state = load_state()
    drops = []
    last_action = 0.0
    lg_present = True
    lg_gone_since = None

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
                    notify("LG display is flapping",
                           "The charge limit is already off. If it keeps dropping, unplug and "
                           "replug the display.")
            except GLib.Error as e:
                log.warning("could not change charge limit: %s", e.message)

        # Undocked: restore the charge limit if we were the ones who turned it off.
        if (state.get("we_disabled_limit") and not lg_present and lg_gone_since is not None
                and now - lg_gone_since >= UNDOCK_GRACE):
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
