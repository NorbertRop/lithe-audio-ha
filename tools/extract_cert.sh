#!/usr/bin/env bash
# Extract the LUCI TLS client certificate from the Lithe Audio Android app.
# Usage: tools/extract_cert.sh <Lithe Audio .apk or .xapk> [output.pem]
set -euo pipefail

src=${1:?usage: $0 <lithe-audio.apk|.xapk> [client.pem]}
out=${2:-client.pem}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

apk=$src
# An XAPK is a zip of split APKs; the base one holds the assets.
if unzip -l "$src" 'com.litheaudio.app.apk' >/dev/null 2>&1; then
  unzip -q -o "$src" 'com.litheaudio.app.apk' -d "$tmp"
  apk="$tmp/com.litheaudio.app.apk"
fi

unzip -q -o "$apk" assets/cert.p12 -d "$tmp"
# Password is hardcoded in the app (SSLContextFactory.getSslBothWayEngine).
openssl pkcs12 -in "$tmp/assets/cert.p12" -passin pass:12345678 -nodes -out "$out" -legacy 2>/dev/null \
  || openssl pkcs12 -in "$tmp/assets/cert.p12" -passin pass:12345678 -nodes -out "$out"
echo "Wrote $out"
