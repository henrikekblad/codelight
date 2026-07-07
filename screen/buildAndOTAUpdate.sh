#!/usr/bin/env bash
# Build the firmware and flash it over the air.
#
# Auto-detects the updater on the device:
#   - codelight firmware      → synchronous updater on :81
#   - codelight bootstrap     → single upload via /flash, eboot does the copy
#   - GeekMagic KR_SDP        → two-step install (bootstrap → codelight), fully automatic
#   - GeekMagic stock (legacy) → ESP8266HTTPUpdateServer form on :80/update
#
# KR_SDP two-step (handled automatically):
#   1. Flash bootstrap via /update_ota (filename must start with "KR_SDP")
#   2. Script connects to claude-screen-setup AP, uploads codelight via /flash
#
# Requires: curl, nmcli, python3 (Linux).
#
# Usage: ./buildAndOTAUpdate.sh [host] [--wifi-ssid SSID --wifi-password PASS]
#        ./buildAndOTAUpdate.sh 192.168.4.1     # bootstrap or KR_SDP in AP mode
#        ./buildAndOTAUpdate.sh 192.168.4.1 --wifi-ssid MyNet --wifi-password s3cr3t
set -euo pipefail
cd "$(dirname "$0")"

HOST=""
WIFI_SSID=""
WIFI_PASS=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --wifi-ssid)     WIFI_SSID="$2"; shift 2 ;;
        --wifi-password) WIFI_PASS="$2"; shift 2 ;;
        -*)              echo "Unknown option: $1" >&2; exit 1 ;;
        *)               HOST="$1"; shift ;;
    esac
done
HOST="${HOST:-claude-screen.local}"
BIN=".pio/build/geekmagic_ultra/firmware.bin"

pio run

# ── WiFi config push ──────────────────────────────────────────────────────────
# Called after codelight boots into AP mode on a first install.
# Connects to the setup AP, POSTs the WiFi credentials via /api/config,
# triggers a reboot via /api/reboot, then reconnects to the original network
# and waits for the device to come up on claude-screen.local.
push_wifi_config() {
    local prev_conn
    prev_conn=$(nmcli -t -f active,connection dev status 2>/dev/null \
                | grep '^yes' | cut -d: -f2 | head -1 || true)

    echo -n "── Waiting for 'claude-screen-setup' AP (up to 40 s) "
    local ap_seen=""
    for _ in $(seq 20); do
        sleep 2
        if nmcli dev wifi list --rescan yes 2>/dev/null | grep -q "claude-screen-setup"; then
            ap_seen=1; echo " found"; break
        fi
        echo -n .
    done
    if [ -z "$ap_seen" ]; then
        echo
        echo "── 'claude-screen-setup' AP did not appear — configure manually at http://192.168.4.1" >&2
        return 1
    fi

    echo "── Connecting to claude-screen-setup..."
    nmcli dev wifi connect "claude-screen-setup" >/dev/null 2>&1 || true

    echo -n "── Waiting for config API "
    local api_up=""
    for _ in $(seq 15); do
        sleep 2
        if curl -sf --max-time 10 -o /dev/null "http://192.168.4.1/" 2>/dev/null; then
            api_up=1; echo " ok"; break
        fi
        echo -n .
    done
    if [ -z "$api_up" ]; then
        echo
        echo "── Could not reach 192.168.4.1 — configure manually at http://192.168.4.1" >&2
        [ -n "$prev_conn" ] && nmcli con up "$prev_conn" >/dev/null 2>&1 || true
        return 1
    fi

    local wifi_json
    wifi_json=$(python3 -c "
import json, sys
print(json.dumps({'wifi':[{'ssid':sys.argv[1],'password':sys.argv[2]}]}))
" "$WIFI_SSID" "$WIFI_PASS")

    echo "── Pushing WiFi config (ssid: $WIFI_SSID)..."
    curl -s --max-time 15 \
        -H "Content-Type: application/json" \
        -d "$wifi_json" \
        "http://192.168.4.1/api/config" >/dev/null || true

    echo "── Triggering reboot..."
    curl -s --max-time 10 -X POST "http://192.168.4.1/api/reboot" >/dev/null || true

    if [ -n "$prev_conn" ]; then
        echo "── Reconnecting to '$prev_conn'..."
        nmcli con up "$prev_conn" >/dev/null 2>&1 || true
        sleep 3
    fi

    echo -n "── Waiting for codelight on claude-screen.local "
    for _ in $(seq 30); do
        sleep 2
        if curl -sf --max-time 10 -o /dev/null "http://claude-screen.local/" 2>/dev/null; then
            echo; echo "── Done — http://claude-screen.local/debug"; return 0
        fi
        echo -n .
    done
    echo
    echo "── Device did not appear as claude-screen.local within 60 s — check it manually" >&2
    return 1
}

# ── Bootstrap path ────────────────────────────────────────────────────────────
# Device is running the codelight bootstrap (after KR_SDP install or esptool flash).
# If HOST is 192.168.4.1 and not yet reachable, connect to the setup AP first.
prev_conn=""
bootstrap_page=$(curl -sf --max-time 10 "http://$HOST/" 2>/dev/null || true)
if [ "$HOST" = "192.168.4.1" ] && [ -z "$bootstrap_page" ]; then
    prev_conn=$(nmcli -t -f active,connection dev status 2>/dev/null \
                | grep '^yes' | cut -d: -f2 | head -1 || true)
    echo "── 192.168.4.1 not reachable — connecting to 'claude-screen-setup'..."
    nmcli dev wifi connect "claude-screen-setup" >/dev/null 2>&1 &
    sleep 1
    echo -n "── Waiting for bootstrap at 192.168.4.1 "
    for _ in $(seq 20); do
        sleep 2
        bootstrap_page=$(curl -sf --max-time 10 "http://$HOST/" 2>/dev/null || true)
        if [ -n "$bootstrap_page" ]; then echo " ok"; break; fi
        echo -n .
    done
    echo
fi

if echo "$bootstrap_page" | grep -qi "bootstrap"; then
    echo "── Bootstrap detected on $HOST"

    # Self-upgrade: if outdated, flash the new bootstrap first.
    # The small bootstrap binary ends below the staging start so the old eboot
    # handles this upload safely regardless of version.
    if ! echo "$bootstrap_page" | grep -qi "bootstrap v8"; then
        echo "── Outdated bootstrap — upgrading to v8 first..."
        BOOTSTRAP_BIN=".pio/build/bootstrap/firmware.bin"
        upg_resp=$(curl -s --max-time 180 -F "fw=@$BOOTSTRAP_BIN" "http://$HOST/flash") || upg_resp=""
        if echo "$upg_resp" | grep -qi "copying firmware"; then
            echo "── Bootstrap upgrade: copying to flash, rebooting..."
        elif [ -z "$upg_resp" ]; then
            echo "   (connection dropped — normal)"
        else
            echo "error: bootstrap upgrade failed: $upg_resp" >&2; exit 1
        fi
        # After reboot the bootstrap is in AP-only mode at 192.168.4.1.
        if [ -z "$prev_conn" ]; then
            prev_conn=$(nmcli -t -f active,connection dev status 2>/dev/null \
                        | grep '^yes' | cut -d: -f2 | head -1 || true)
        fi
        sleep 3
        echo "── Connecting to claude-screen-setup for v8 bootstrap..."
        nmcli dev wifi connect "claude-screen-setup" >/dev/null 2>&1 || true
        HOST="192.168.4.1"
        echo -n "── Waiting for bootstrap v8 at 192.168.4.1 "
        bootstrap_page=""
        for _ in $(seq 20); do
            sleep 2
            bootstrap_page=$(curl -sf --max-time 10 "http://$HOST/" 2>/dev/null || true)
            if echo "$bootstrap_page" | grep -qi "bootstrap v8"; then echo " ok"; break; fi
            echo -n .
        done
        echo
        if ! echo "$bootstrap_page" | grep -qi "bootstrap v8"; then
            echo "error: bootstrap v8 did not appear — upgrade may have failed" >&2
            exit 1
        fi
        echo "── Bootstrap upgraded to v8"
    fi

    echo "── Uploading $(stat -c %s "$BIN") bytes to $HOST..."
    resp=$(curl -s --max-time 300 -F "fw=@$BIN" "http://$HOST/flash") || resp=""
    if echo "$resp" | grep -qi "copying firmware"; then
        echo "── Firmware staged, device rebooting..."
    elif [ -z "$resp" ]; then
        echo "   (connection dropped during reboot — this is normal)"
    else
        echo "error: unexpected response: $resp" >&2; exit 1
    fi

    if [ -n "$prev_conn" ]; then
        echo "── Reconnecting to '$prev_conn'..."
        nmcli con up "$prev_conn" >/dev/null 2>&1 || true
        sleep 3
    fi

    echo -n "── Waiting for codelight on claude-screen.local "
    for _ in $(seq 30); do
        sleep 2
        if curl -sf --max-time 10 -o /dev/null "http://claude-screen.local/" 2>/dev/null; then
            echo; echo "── Done — http://claude-screen.local/debug"; exit 0
        fi
        echo -n .
    done
    echo
    echo "── Did not appear as claude-screen.local — first-run AP mode."
    if [ -n "$WIFI_SSID" ]; then
        push_wifi_config
    else
        echo "   Connect to 'claude-screen-setup' and open http://192.168.4.1"
    fi
    exit 0
fi

# ── Codelight path (:81 synchronous updater) ─────────────────────────────────
# Probe with retries: right after boot the loop can be inside a blocking mDNS
# query for a few seconds.
sync_ok=""
for _ in 1 2 3; do
    if curl -sf --max-time 5 -o /dev/null "http://$HOST:81/update"; then sync_ok=1; break; fi
    sleep 2
done
if [ -n "$sync_ok" ]; then
    echo "── Synchronous updater found on :81"
    ok=""
    for attempt in 1 2 3; do
        resp=$(curl -s --max-time 300 -F "firmware=@$BIN" "http://$HOST:81/update") || true
        if grep -qi "Update Success" <<<"$resp"; then ok=1; break; fi
        echo "   attempt $attempt: ${resp:-connection dropped}" >&2
        sleep 3
    done
    if [ -z "$ok" ]; then
        echo "error: giving up after 3 attempts" >&2
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

# ── KR_SDP path (two-step install) ───────────────────────────────────────────
echo "── Probing http://$HOST/update_ota ..."
probe_file=$(mktemp)
echo "test" > "$probe_file"
probe_code=$(curl -s -o /dev/null -w "%{http_code}" -F "update=@$probe_file" "http://$HOST/update_ota" 2>/dev/null)
rm -f "$probe_file"

if [ "$probe_code" != "404" ] && [ -n "$probe_code" ]; then
    echo "── KR_SDP stock firmware detected — running two-step install"
    BOOTSTRAP_BIN=".pio/build/bootstrap/firmware.bin"

    # Step 1: flash bootstrap via /update_ota.
    # Filename MUST start with "KR_SDP" or the stock firmware rejects the upload.
    echo "── Step 1/2: flashing bootstrap..."
    ota_resp=$(curl -s --max-time 60 \
        -F "update=@$BOOTSTRAP_BIN;filename=KR_SDP_bootstrap.bin" \
        "http://$HOST/update_ota" 2>/dev/null || true)
    echo "   /update_ota response: ${ota_resp:-(empty)}"

    echo -n "── Waiting for 'claude-screen-setup' AP (up to 40 s) "
    bootstrap_ap=""
    for _ in $(seq 20); do
        sleep 2
        if nmcli dev wifi list --rescan yes 2>/dev/null | grep -q "claude-screen-setup"; then
            bootstrap_ap=1; break
        fi
        echo -n .
    done
    echo
    if [ -z "$bootstrap_ap" ]; then
        echo "error: claude-screen-setup AP did not appear — bootstrap flash may have failed" >&2
        exit 1
    fi

    # Step 2: connect to bootstrap AP, upload codelight.
    echo "── Step 2/2: connecting to bootstrap AP..."
    prev_conn=$(nmcli -t -f active,connection dev status 2>/dev/null \
                | grep '^yes' | cut -d: -f2 | head -1 || true)
    nmcli dev wifi connect "claude-screen-setup" >/dev/null 2>&1 &
    sleep 1
    echo -n "── Waiting for bootstrap to respond "
    for _ in $(seq 20); do
        sleep 2
        if curl -sf --max-time 10 -o /dev/null "http://192.168.4.1/" 2>/dev/null; then
            echo " ok"; break
        fi
        echo -n .
    done

    echo "── Uploading codelight firmware ($(stat -c %s "$BIN") bytes) to 192.168.4.1..."
    resp=$(curl -s --max-time 300 -F "fw=@$BIN" "http://192.168.4.1/flash") || resp=""

    if [ -n "$prev_conn" ]; then
        echo "── Reconnecting to '$prev_conn'..."
        nmcli con up "$prev_conn" >/dev/null 2>&1 || true
        sleep 3
    fi

    if echo "$resp" | grep -qi "copying firmware"; then
        echo "── Firmware staged, device rebooting..."
    elif [ -z "$resp" ]; then
        echo "   (connection dropped during reboot — this is normal)"
    else
        echo "error: unexpected response: $resp" >&2
        exit 1
    fi

    echo -n "── Waiting for codelight to come up on claude-screen.local "
    for _ in $(seq 30); do
        sleep 2
        if curl -sf --max-time 2 -o /dev/null "http://claude-screen.local/" 2>/dev/null; then
            echo
            echo "── Done — codelight is up: http://claude-screen.local/debug"
            exit 0
        fi
        echo -n .
    done
    echo
    echo "── Device did not appear as claude-screen.local within 60 s."
    if [ -n "$WIFI_SSID" ]; then
        push_wifi_config
    else
        echo "   Connect to 'claude-screen-setup' WiFi and open http://192.168.4.1 to configure."
    fi
    exit 0
fi

# ── Legacy stock firmware path ────────────────────────────────────────────────
echo "── Probing http://$HOST/update ..."
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
if [ -n "$WIFI_SSID" ]; then
    push_wifi_config
else
    echo "── First flash: connect to the 'claude-screen-setup' WiFi AP and"
    echo "   open http://192.168.4.1 to configure. See README.md."
fi
exit 0
