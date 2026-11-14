#!/usr/bin/env bash
# Reproduce "LG UltraFine 5K drops when the laptop stops charging" on demand.
#
# Trigger: enable the UPower charge threshold (stop at 80%) while the battery
# is above 80% and charging. The EC stops charging immediately; on this
# PX13 + LG combination that has always dropped the Thunderbolt link.
#
# Usage: [FORCE=1] lg-charge-test.sh [watch_seconds]   (default 60; FORCE skips the >80% Charging precheck)
# Requirements: LG connected, battery > 80% and Charging.
set -u
WATCH=${1:-60}
BAT=/sys/class/power_supply/BAT0
UPD=/org/freedesktop/UPower/devices/battery_BAT0
LOG=${LG_TEST_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/lg-charge-test-$(date +%Y%m%d-%H%M%S).log}

drops() { journalctl -b 0 -k --no-pager -o cat | grep -cE 'thunderbolt [01]-2: device disconnected'; }
lg_connected() { journalctl -b 0 -k --no-pager -o cat | grep -E 'thunderbolt [01]-2: (LG Electronics|device disconnected)' | tail -1 | grep -q 'LG Electronics'; }
sources() {
    local out="" p
    for p in /sys/class/power_supply/*; do
        [ -f "$p/online" ] || continue
        case "$(cat "$p/type")" in Mains|USB) out+="$(basename "$p")=$(cat "$p/online") ";; esac
    done
    echo "$out"
}
threshold() {
    gdbus call --system --dest org.freedesktop.UPower --object-path "$UPD" \
        --method org.freedesktop.UPower.Device.EnableChargeThreshold "$1" >/dev/null
}
state() { echo "$(date +%T.%3N) bat=$(cat $BAT/capacity)% $(cat $BAT/status) limit=$(cat $BAT/charge_control_end_threshold) drops=$(( $(drops) - BASE )) $(sources)"; }

mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
echo "== LG charge-stop test, log: $LOG"

lg_connected || { echo "ABORT: LG not connected"; exit 2; }
cap=$(cat $BAT/capacity); st=$(cat $BAT/status)
if [ "${FORCE:-0}" != 1 ] && { [ "$cap" -le 80 ] || [ "$st" != Charging ]; }; then
    echo "ABORT: need battery > 80% and Charging (now ${cap}% $st)."
    echo "       Run on battery for a while, then plug in so it starts charging."
    exit 2
fi

BASE=$(drops)
echo "precheck OK"; state
echo "-- trigger: enabling charge limit (stop at 80%)"
threshold true
trigger=$(date +%s)

result=SURVIVED
for ((i = 0; i < WATCH; i++)); do
    state
    if [ $(( $(drops) - BASE )) -gt 0 ]; then
        result="DROPPED after $(( $(date +%s) - trigger )) s"
        break
    fi
    sleep 1
done

echo "-- restoring: charge limit off"
threshold false
state
echo "== RESULT: $result"
echo "-- kernel log around the trigger:"
journalctl -b 0 -k --no-pager -o short-precise --since "@$((trigger - 2))" \
    | grep -E 'retimer|device disconnected|hot unplug|pm: AC|asus_wmi|ucsi' | head -15
