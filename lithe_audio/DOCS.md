# Lithe Audio

Local control of Lithe Audio Wi-Fi speakers (Wi-Fi v3, V2 ceiling, PRO, iO1) without the
Lithe Audio phone app.

## What you get

- **Lithe Audio** sidebar panel (ingress) with volume, transport, treble/mid/bass/balance,
  preset, output mode, loudness, night mode, high-pass filter, Bluetooth and AUX toggles.
- With the Mosquitto broker installed, one Home Assistant device per speaker with matching
  `number`, `select`, `switch`, `button` and `sensor` entities (MQTT discovery).

Playback itself (media player, TTS, Music Assistant) keeps going through the speaker's
Google Cast / AirPlay integrations; this add-on covers what only the Lithe app could do.

## Client certificate

Required. Copy `client.pem` (from `tools/extract_cert.sh` in the repository) into this
add-on's config folder, `/addon_configs/<id>_lithe_audio/client.pem`, and restart.
Until it's there the log shows `client.pem not found`.

## Configuration

| Option | Default | Meaning |
| --- | --- | --- |
| `discovery` | `true` | Find speakers via mDNS (`_airplay._tcp`, manufacturer "Lithe Audio"). |
| `speakers` | `[]` | Extra IPs or hostnames to always connect to. |
| `mqtt_enabled` | `true` | Publish MQTT discovery entities. |
| `mqtt_host` … | empty | Override broker; empty uses the Mosquitto add-on. |

Give each speaker a DHCP reservation if you list it under `speakers`.

## Protocol notes

The speakers run a Libre Wireless LS10 module:

- **LUCI**, TCP 7777, mutual TLS with the client certificate shipped in the official app.
  Message boxes: 3 register, 40 play control, 42 now-playing JSON, 64 volume (0–100),
  90 name, 91 MAC, 111 tunnel start.
- **Tunnel**, TCP 50007, plaintext, relayed to the amplifier MCU after LUCI box 111 is
  set to `50007`. Packet: `len(2, BE) checksum(1) cmd(2) data…`, checksum = sum of bytes
  from `cmd` on. EQ values are −6…+6, negatives encoded as `0xF0 | magnitude`.
