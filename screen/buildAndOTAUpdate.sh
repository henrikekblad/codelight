#!/usr/bin/env bash
# Build the firmware and flash it over the air.
#
# Auto-detects the updater on the device:
#   - codelight firmware → synchronous updater on :81
#   - GeekMagic stock    → ESP8266HTTPUpdateServer form on :80 (gzipped upload)
#
# Usage: ./buildAndOTAUpdate.sh [host]     default host: claude-screen.local
#        ./buildAndOTAUpdate.sh 192.168.4.1     # stock firmware in AP mode
set -euo pipefail
cd "$(dirname "$0")"

HOST="${1:-claude-screen.local}"
BIN=".pio/build/geekmagic_ultra/firmware.bin"

pio run

# Preferred path: the firmware's synchronous updater on :81 — the async (:80)
# stack on ESP8266 is unreliable for large uploads. Probe with retries: right
# after boot the loop can be inside a blocking mDNS query for a few seconds.
sync_ok=""
for _ in 1 2 3; do
    if curl -sf --max-time 5 -o /dev/null "http://$HOST:81/update"; then sync_ok=1; break; fi
    sleep 2
done
if [ -n "$sync_ok" ]; then
    echo "── Synchronous updater found on :81"
    resp=$(curl -s --max-time 300 -F "firmware=@$BIN" "http://$HOST:81/update") || true
    if ! grep -qi "Update Success" <<<"$resp"; then
        echo "error: unexpected response: ${resp:-connection dropped}" >&2
        exit 1
    fi
    echo "── Uploaded $(stat -c %s "$BIN") bytes, device is rebooting"
    echo -n "── Waiting for http://$HOST to come back "
    for _ in $(seq 30); do
        sleep 2
        if curl -sf --max-time 2 -o /dev/null "http://$HOST/"; then
            echo
            echo "── Done — device is up: http://$HOST/debug"
            exit 0
        fi
        echo -n .
    done
    echo
    echo "warning: device did not respond within 60 s — check it manually" >&2
    exit 1
fi

echo "── Probing http://$HOST/update ..."
# The stock updater serves a plain HTML upload form on :80; current codelight
# firmware was handled above via :81.
page=$(curl -sf --compressed --max-time 10 "http://$HOST/update") || {
    echo "error: cannot reach http://$HOST/update" >&2
    exit 1
}

if ! grep -qiE "name='firmware'|multipart/form-data" <<<"$page"; then
    echo "error: no known updater found on $HOST." >&2
    echo "       A device on legacy codelight firmware (<= v1.0.11) can be" >&2
    echo "       updated once via its browser page at http://$HOST/update." >&2
    exit 1
fi

echo "── Stock firmware updater detected (ESP8266HTTPUpdateServer)"
# Stock uses a large-filesystem flash layout leaving only ~520 KB of OTA
# staging space — the plain bin (~600 KB) fails with "Not Enough Space".
# Upload gzipped instead; eboot expands it during the boot copy.
gzip -9 -kf "$BIN"
resp=$(curl -s --max-time 180 -F "firmware=@$BIN.gz" "http://$HOST/update") || true
if ! grep -q "Update Success" <<<"$resp"; then
    echo "error: unexpected response: ${resp:-connection dropped}" >&2
    exit 1
fi

echo "── Uploaded $(stat -c %s "$BIN.gz") bytes, device is rebooting"
# The device reboots into codelight with no config and opens its setup AP,
# so it won't come back on this address.
echo "── First flash: connect to the 'claude-screen-setup' WiFi AP and"
echo "   open http://192.168.4.1 to configure. See README.md."
exit 0
