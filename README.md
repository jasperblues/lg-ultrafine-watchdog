# lg-ultrafine-watchdog

**Keep an LG UltraFine 5K (Thunderbolt 3) usable on a Linux laptop whose firmware
drops the Thunderbolt link when charging stops.** A tiny user service watches for the
display flapping (disconnecting and reconnecting every few seconds) and cures it by
turning the battery charge limit off, which makes the laptop draw power again and the
display settle. When you undock, it turns the charge limit back on.

```sh
git clone https://github.com/jasperblues/lg-ultrafine-watchdog ~/projects/lg-ultrafine-watchdog
systemctl --user enable --now ~/projects/lg-ultrafine-watchdog/lg-watchdog.service
```

No root needed: the charge limit is changed through UPower, the same switch as
GNOME's *Settings → Power → Preserve Battery Health*.

## The problem

Found on an ASUS ProArt PX13 (HN7306EAC, Ryzen AI Max+ 395, BIOS 314) running
Fedora 45, with an LG UltraFine 5K that also charges the laptop over the same cable:

- Whenever the laptop **stops charging** — reaching the charge limit (e.g. 80%),
  reaching 100%, or being plugged in while already above the limit — the USB4
  retimer on the laptop's port disconnects and the display drops.
- Usually it comes back after a ~2 s blink. Sometimes it gets **stuck flapping**,
  reconnecting and dropping every few seconds. Plugging in while above the charge
  limit reliably causes this.
- Nothing in Linux initiates it: the kernel's UCSI trace shows no command or event
  before the drop. It happens in the laptop's firmware / USB-C power negotiation.
  The same display and cable are stable on a MacBook Pro.
- On this laptop, one USB-C port is much more forgiving than the other (charger
  plug/unplug and charging start don't affect it). Try both.

**What cures the flapping:** giving the laptop a reason to draw power again.
Turning the charge limit off stops it instantly while the battery isn't full;
plugging in the laptop's own (barrel) charger stops it instantly in every case.
Restarting the Thunderbolt controller does not.

**Most stable setup found:** laptop charger plugged in *and* the display on the
more forgiving USB-C port. That survived a full battery, charge-limit changes and
charger plug/unplug.

## What the watchdog does

1. Follows the kernel log (`journalctl -k -f`) for
   `thunderbolt N-2: device disconnected`.
2. On **3 or more drops within 30 seconds**, turns the charge limit **off** via
   UPower and shows a desktop notification. At most once every 5 minutes.
3. If the limit is already off (battery full), it notifies you to plug in the
   laptop charger, which stops the flapping.
4. While docked, holds a systemd-logind `handle-lid-switch` inhibitor, so a display
   drop with the lid closed can't send the laptop to sleep (and wake into a mess).
5. When the display has been gone for **60 seconds** (you undocked), releases the
   inhibitor and turns the charge limit back **on** — but only if the watchdog was
   the one that turned it off. State lives in `~/.local/state/lg-watchdog.json`.

Logs: `journalctl --user -u lg-watchdog -f`.

## Reproducing the problem

`lg-charge-test.sh` triggers it on demand: with the display connected, the battery
above 80% and charging, it enables the 80% limit (charging stops at once), watches
for drops for 60 s, then turns the limit off again and prints the relevant kernel
log lines.

```sh
./lg-charge-test.sh          # needs battery > 80% and Charging
FORCE=1 ./lg-charge-test.sh  # skip the precheck
```

## Other Linux fixes that were needed on the same setup

Not part of this repo, but they cost hours to find:

- **Closed-lid (clamshell) mode at 5K fallback:** with the lid closed GNOME had no
  saved layout for "external display only" and fell back to the LG's preferred
  tiled 5K mode, which this laptop can't drive. Fix: save a 4K layout for the
  lid-closed case in `~/.config/monitors.xml`.
- **Keyboards dead with the lid closed (with Toshy):** the AMD Sensor Fusion Hub
  reports *tablet mode* about a second after the lid closes, and libinput then
  disables internal keyboards — which includes Toshy's virtual keyboard. Fix: a
  libinput quirk hiding `SW_TABLET_MODE` from the `AMD SFH tablet mode switch`.
- **The LG's USB/speakers sometimes don't appear:** the PCIe tunnel comes up but
  nothing is enumerated; `echo 1 | sudo tee /sys/bus/pci/rescan` brings them up.
  `extras/` has a udev rule + helper that does this automatically (only when the
  LG's USB controller is missing, and never while it is flapping):
  `sudo install -m755 extras/lg-usb-ensure /usr/local/sbin/ && sudo install -m644
  extras/91-lg-ultrafine-usb.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules`
- **Don't open the LG's built-in camera** if it's failing: on this unit, opening it
  crashed the display's data side and took the link down.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
