# Lithe Audio for Home Assistant

Home Assistant add-on that replaces the Lithe Audio phone app's settings screen:
EQ (treble/mid/bass), balance, presets, loudness, night mode, high-pass filter,
output mode, Bluetooth/AUX inputs, volume and transport, all over the local network.

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FNorbertRop%2Flithe-audio-ha)

## Install

1. *Settings → Add-ons → Add-on store → ⋮ → Repositories*, add
   `https://github.com/NorbertRop/lithe-audio-ha` (or use the button above).
2. Install **Lithe Audio** and start it once; this creates its config folder.
3. Provide the client certificate (below), then restart the add-on.

## Client certificate

The speakers only accept control connections that present the TLS client certificate
bundled in the official Lithe Audio Android app. It isn't in this repository; extract
it from the app yourself:

```bash
tools/extract_cert.sh ~/Downloads/Lithe+Audio_*.xapk client.pem
```

Copy `client.pem` into the add-on's config folder, `/addon_configs/<id>_lithe_audio/`
on the HA host (visible as the `addon_configs` share in the Samba add-on, or through the
File editor / Studio Code Server add-ons).

See [lithe_audio/DOCS.md](lithe_audio/DOCS.md) for options and protocol details.

## Local development

```bash
cp client.pem lithe_audio/app/certs/
cd lithe_audio/app
uv run --no-project --with-requirements requirements.txt main.py
```

With no `/data/options.json`, it discovers speakers via mDNS and serves the UI on
<http://localhost:8099>. Set `OPTIONS_FILE` to point at a JSON file with the add-on options.
