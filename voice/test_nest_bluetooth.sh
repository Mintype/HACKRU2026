#!/usr/bin/env bash
# Test Bluetooth audio from the Raspberry Pi 5 to a Google Nest.
#
# Run on the Pi as your normal desktop user (NOT with sudo): PipeWire audio
# runs per user, so a root shell can't see the Nest as an audio output.
#
# Usage:
#   ./test_nest_bluetooth.sh                  # scan, pick the Nest, pair, play a tone
#   ./test_nest_bluetooth.sh AA:BB:CC:DD:EE:FF  # skip the scan
#   ./test_nest_bluetooth.sh AA:BB:CC:DD:EE:FF --idle 10
#                                             # also replay after 10 min of silence
#
# Before running: Google Home app -> the Nest -> Settings -> Audio ->
# Paired Bluetooth devices -> Enable pairing mode.

set -u

MAC="${1:-}"
IDLE_MIN=0
if [[ "${2:-}" == "--idle" ]]; then
  IDLE_MIN="${3:-10}"
fi

TEST_WAV="$(mktemp /tmp/nest_test_XXXX.wav)"
trap 'rm -f "$TEST_WAV"' EXIT

ok()   { echo "  [OK]   $*"; }
fail() { echo "  [FAIL] $*"; exit 1; }
info() { echo "  ...    $*"; }

if [[ $EUID -eq 0 ]]; then
  fail "Don't run this with sudo; run it as your normal user."
fi

echo "1. Bluetooth adapter"
command -v bluetoothctl >/dev/null || fail "bluetoothctl not found (sudo apt install bluez)"
systemctl is-active --quiet bluetooth || fail "bluetooth service not running (sudo systemctl enable --now bluetooth)"
if ! bluetoothctl show | grep -q "Powered: yes"; then
  info "adapter is off, turning it on"
  rfkill unblock bluetooth 2>/dev/null
  bluetoothctl power on >/dev/null
  sleep 1
  bluetoothctl show | grep -q "Powered: yes" || fail "could not power on the Bluetooth adapter"
fi
ok "adapter powered on"

echo "2. Audio system"
command -v pactl >/dev/null || fail "pactl not found: sudo apt install pulseaudio-utils libspa-0.2-bluetooth (on Pi OS Lite, also pipewire pipewire-pulse wireplumber)"
pactl info 2>/dev/null | grep -qi "pipewire" || fail "PipeWire isn't running for this user (are you logged in to the desktop / using sudo?)"
ok "PipeWire is running"

echo "3. Find the Nest"
if [[ -z "$MAC" ]]; then
  info "scanning for 20 seconds (make sure the Nest is in pairing mode)..."
  bluetoothctl --timeout 20 scan on >/dev/null 2>&1
  echo
  echo "  Devices seen (the Nest uses the name you gave it in Google Home):"
  mapfile -t DEVICES < <(bluetoothctl devices | sed 's/^Device //')
  [[ ${#DEVICES[@]} -gt 0 ]] || fail "no devices found. Is pairing mode on? It times out after a few minutes."
  for i in "${!DEVICES[@]}"; do
    echo "    [$i] ${DEVICES[$i]}"
  done
  read -rp "  Pick the Nest's number: " PICK
  MAC="$(echo "${DEVICES[$PICK]}" | awk '{print $1}')"
fi
[[ "$MAC" =~ ^([0-9A-F]{2}:){5}[0-9A-F]{2}$ ]] || fail "'$MAC' doesn't look like a Bluetooth address"
ok "using $MAC  (put this in .env as NEST_BLUETOOTH_MAC)"

echo "4. Pair, trust, connect"
if bluetoothctl info "$MAC" | grep -q "Paired: yes"; then
  ok "already paired"
else
  info "pairing (keep the Nest in pairing mode)..."
  # Keep scanning while pairing: BlueZ often can't pair a device it hasn't seen recently.
  bluetoothctl --timeout 30 scan on >/dev/null 2>&1 &
  SCAN_PID=$!
  sleep 5
  PAIR_OUT="$(bluetoothctl --agent NoInputNoOutput pair "$MAC" 2>&1)"
  kill "$SCAN_PID" 2>/dev/null
  if ! bluetoothctl info "$MAC" | grep -q "Paired: yes"; then
    echo "$PAIR_OUT" | grep -iE "fail|error" | sed 's/^/         /'
    fail "pairing failed. Re-enable pairing mode on the Nest and try again, or pair by hand: bluetoothctl -> agent on -> default-agent -> scan on -> pair $MAC"
  fi
  ok "paired"
fi
bluetoothctl trust "$MAC" >/dev/null && ok "trusted (Pi can auto-reconnect)"

connect_nest() {
  if bluetoothctl info "$MAC" | grep -q "Connected: yes"; then
    return 0
  fi
  for attempt in 1 2 3; do
    bluetoothctl connect "$MAC" >/dev/null 2>&1
    sleep 2
    bluetoothctl info "$MAC" | grep -q "Connected: yes" && return 0
    info "connect attempt $attempt failed, retrying"
  done
  return 1
}
connect_nest || fail "could not connect to the Nest"
ok "connected"

echo "5. Nest as audio output"
SINK=""
for _ in {1..10}; do
  SINK="$(pactl list short sinks | awk '{print $2}' | grep -i "bluez" | grep -i "${MAC//:/_}" | head -n1)"
  [[ -n "$SINK" ]] && break
  sleep 1
done
[[ -n "$SINK" ]] || fail "connected, but no audio output appeared for the Nest. Check: pactl list short sinks"
pactl set-default-sink "$SINK" && ok "default output is now $SINK"

echo "6. Play a test sound"
# 0.5 s of silence first: Bluetooth speakers often clip the start while the link wakes up.
python3 - "$TEST_WAV" <<'EOF'
import math, struct, sys, wave
rate = 24000
samples = [0] * int(rate * 0.5)
for note in (523.25, 659.25, 783.99):          # C-E-G chime
    for i in range(int(rate * 0.3)):
        fade = min(1.0, i / 600, (rate * 0.3 - i) / 600)
        samples.append(int(12000 * fade * math.sin(2 * math.pi * note * i / rate)))
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
EOF

play_test() {
  if command -v pw-play >/dev/null; then
    pw-play --target "$SINK" "$TEST_WAV"
  else
    paplay --device="$SINK" "$TEST_WAV"
  fi
}
play_test || fail "playback command failed"
read -rp "  Did you hear a 3-note chime from the Nest? [y/n] " HEARD
[[ "$HEARD" == [yY]* ]] || fail "no sound: check the Nest's volume (say 'Hey Google, volume 6') and try again"
ok "audio plays on the Nest"

if [[ "$IDLE_MIN" -gt 0 ]]; then
  echo "7. Idle test: waiting $IDLE_MIN minutes of silence, then replaying"
  sleep $((IDLE_MIN * 60))
  if bluetoothctl info "$MAC" | grep -q "Connected: yes"; then
    ok "link stayed connected while idle"
  else
    info "link dropped while idle (expected sometimes); reconnecting"
    connect_nest || fail "could not reconnect after idle; the voice code must handle this or use Wi-Fi cast"
    ok "reconnected"
  fi
  play_test
  read -rp "  Did you hear the chime again? [y/n] " HEARD
  [[ "$HEARD" == [yY]* ]] || fail "no sound after idle"
  ok "audio still works after $IDLE_MIN minutes idle"
fi

echo
echo "All checks passed. Set NEST_BLUETOOTH_MAC=$MAC in .env   (sink: $SINK)"
