"""Lithe Audio (Libre Wireless LS10) local control protocol.

Two channels, both reverse engineered from the official Android app
(com.litheaudio.app, Hansong ``librecontroller`` SDK):

* LUCI on TCP 7777 over mutual TLS: volume, transport, metadata, device info.
* Tunnel on TCP 50007, plaintext, forwarded to the amplifier MCU: EQ and
  speaker settings. The speaker only answers on it after LUCI message box
  111 (tunneling start) has been sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import struct
from collections.abc import Callable
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

LUCI_PORT = 7777
TUNNEL_PORT = 50007
# /config is the add-on's addon_config folder; certs/ is for local development.
CERT_FILES = (Path("/config/client.pem"), Path(__file__).parent / "certs" / "client.pem")

LUCI_GET = 1
LUCI_SET = 2

MB_REGISTER = 3
MB_FIRMWARE = 5
MB_PLAY_CONTROL = 40
MB_GET_UI = 42
MB_PLAY_ELAPSED = 49
MB_SOURCE = 50
MB_PLAY_STATUS = 51
MB_VOLUME = 64
MB_DEVICE_NAME = 90
MB_MAC = 91
MB_TUNNEL_START = 111
MB_ENV = 208

PLAY_STATUS = {0: "playing", 1: "stopped", 2: "paused", 3: "connecting", 4: "receiving", 5: "buffering"}
PLAY_COMMANDS = {"play": "PLAY", "pause": "PAUSE", "stop": "STOP", "next": "NEXT", "previous": "PREV", "resume": "RESUME"}

PRESETS = ["Normal", "Acoustic", "Jazz", "Pop", "Hip-Hop", "Dance", "Custom"]
# The app writes these treble/mid/bass values right after PRESET_SET.
PRESET_EQ = {1: (2, 2, 2), 2: (3, -2, 2), 3: (2, 3, -2), 4: (-3, 0, 2)}
OUTPUTS = {1: "Stereo", 2: "Left", 3: "Right"}

EQ, BOOL, RAW = "eq", "bool", "raw"

# name: (get command, set command, value kind)
FIELDS: dict[str, tuple[tuple[int, int], tuple[int, int], str]] = {
    "treble": ((9, 1), (9, 2), EQ),
    "mid": ((9, 3), (9, 4), EQ),
    "bass": ((9, 5), (9, 6), EQ),
    "balance": ((14, 1), (14, 2), EQ),
    "preset": ((10, 1), (10, 2), RAW),
    "loudness": ((11, 1), (11, 2), BOOL),
    "night_mode": ((12, 1), (12, 2), BOOL),
    "high_pass_filter": ((13, 1), (13, 2), BOOL),
    "output": ((15, 1), (15, 2), RAW),
    "bluetooth": ((32, 1), (32, 2), BOOL),
    "aux_in": ((33, 1), (33, 2), BOOL),
}
_BY_COMMAND = {cmd: name for name, (get, set_, _) in FIELDS.items() for cmd in (get, set_)}

POLL_INTERVAL = 30
# The MCU drops tunnel commands that arrive back to back.
TUNNEL_SPACING = 0.12
MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
RECONNECT_DELAY = 10


def encode_eq(value: int) -> int:
    """-6..6 -> wire byte; negatives are 0xF0 | magnitude."""
    value = max(-6, min(6, int(value)))
    return value if value >= 0 else 0xF0 - value


def decode_eq(byte: int) -> int:
    return -(byte - 0xF0) if byte >= 0xF0 else byte


def luci_packet(mb: int, cmd_type: int, data: str | bytes = b"") -> bytes:
    if isinstance(data, str):
        data = data.encode()
    return struct.pack("<HBHBHH", 0, cmd_type, mb, 0, 0, len(data)) + data


def tunnel_packet(cmd: tuple[int, int], data: bytes = b"") -> bytes:
    length = len(data) + 3
    packet = bytearray([length >> 8, length & 0xFF, 0, cmd[0], cmd[1]]) + data
    packet[2] = sum(packet[3:]) & 0xFF
    return bytes(packet)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    cert = next((p for p in CERT_FILES if p.exists()), None)
    if cert is None:
        raise FileNotFoundError("client.pem not found; copy it into the add-on config folder (see DOCS.md)")
    ctx.load_cert_chain(cert)
    return ctx


class LitheSpeaker:
    def __init__(self, host: str, on_update: Callable[[LitheSpeaker], None]) -> None:
        self.host = host
        self.available = False
        self.info: dict[str, str] = {}
        self.state: dict[str, object] = {}
        self._on_update = on_update
        self._luci: asyncio.StreamWriter | None = None
        self._tunnel: asyncio.StreamWriter | None = None
        self._tunnel_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    @property
    def id(self) -> str:
        mac = self.info.get("mac")
        return mac.replace(":", "").lower() if mac else self.host.replace(".", "_")

    @property
    def name(self) -> str:
        return self.info.get("name") or self.host

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"lithe-{self.host}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def as_dict(self) -> dict:
        return {"id": self.id, "host": self.host, "available": self.available, **self.info, **self.state}

    # --- commands -------------------------------------------------------

    async def set_field(self, name: str, value) -> None:
        get, set_, kind = FIELDS[name]
        if kind == EQ:
            byte = encode_eq(value)
        elif kind == BOOL:
            byte = 1 if value in (True, 1, "1", "on", "ON", "true") else 0
        elif name == "preset":
            byte = PRESETS.index(value) if isinstance(value, str) else int(value)
        elif name == "output":
            byte = {v: k for k, v in OUTPUTS.items()}[value] if isinstance(value, str) else int(value)
        else:
            byte = int(value)
        self._send_tunnel(set_, bytes([byte]))
        if name == "preset" and byte in PRESET_EQ:
            for field, eq in zip(("treble", "mid", "bass"), PRESET_EQ[byte]):
                self._send_tunnel(FIELDS[field][1], bytes([encode_eq(eq)]))
        self._send_tunnel(get)
        if name == "preset":
            for field in ("treble", "mid", "bass"):
                self._send_tunnel(FIELDS[field][0])

    def set_volume(self, volume: int) -> None:
        self._send_luci(MB_VOLUME, LUCI_SET, str(max(0, min(100, int(volume)))))

    def play_control(self, command: str) -> None:
        self._send_luci(MB_PLAY_CONTROL, LUCI_SET, PLAY_COMMANDS[command])

    def refresh(self) -> None:
        for mb in (MB_VOLUME, MB_PLAY_STATUS, MB_SOURCE, MB_GET_UI):
            self._send_luci(mb, LUCI_GET)
        for get, _, _ in FIELDS.values():
            self._send_tunnel(get)

    # --- connection -----------------------------------------------------

    def _send_luci(self, mb: int, cmd_type: int, data: str | bytes = b"") -> None:
        if not self._luci:
            raise ConnectionError(f"{self.host} not connected")
        self._luci.write(luci_packet(mb, cmd_type, data))

    def _send_tunnel(self, cmd: tuple[int, int], data: bytes = b"") -> None:
        if not self._tunnel:
            raise ConnectionError(f"{self.host} tunnel not connected")
        self._tunnel_queue.put_nowait(tunnel_packet(cmd, data))

    def _notify(self) -> None:
        try:
            self._on_update(self)
        except Exception:
            _LOGGER.exception("update callback failed")

    async def _run(self) -> None:
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, ssl.SSLError, asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as err:
                _LOGGER.warning("%s: connection lost (%s), retrying in %ss", self.host, err, RECONNECT_DELAY)
            finally:
                for writer in (self._luci, self._tunnel):
                    if writer:
                        writer.close()
                self._luci = self._tunnel = None
                if self.available:
                    self.available = False
                    self._notify()
            await asyncio.sleep(RECONNECT_DELAY)

    async def _session(self) -> None:
        luci_r, self._luci = await asyncio.wait_for(
            asyncio.open_connection(self.host, LUCI_PORT, ssl=_ssl_context()), 10
        )
        local_ip = self._luci.get_extra_info("sockname")[0]
        self._send_luci(MB_REGISTER, LUCI_SET, local_ip)
        self._send_luci(MB_TUNNEL_START, LUCI_SET, str(TUNNEL_PORT))
        for mb in (MB_DEVICE_NAME, MB_MAC, MB_FIRMWARE):
            self._send_luci(mb, LUCI_GET)
        self._send_luci(MB_ENV, LUCI_GET, "READ_Model")
        await asyncio.sleep(0.5)

        tunnel_r, self._tunnel = await asyncio.wait_for(asyncio.open_connection(self.host, TUNNEL_PORT), 10)
        _LOGGER.info("%s: connected", self.host)
        self._tunnel_queue = asyncio.Queue()
        self.available = True
        self.refresh()

        tasks = [
            asyncio.create_task(self._read_luci(luci_r)),
            asyncio.create_task(self._read_tunnel(tunnel_r)),
            asyncio.create_task(self._write_tunnel()),
            asyncio.create_task(self._poll()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise ConnectionError("stream closed")
        finally:
            for task in tasks:
                task.cancel()

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            self.refresh()
            await self._luci.drain()

    async def _write_tunnel(self) -> None:
        while True:
            self._tunnel.write(await self._tunnel_queue.get())
            await self._tunnel.drain()
            await asyncio.sleep(TUNNEL_SPACING)

    async def _read_luci(self, reader: asyncio.StreamReader) -> None:
        while True:
            header = await reader.readexactly(10)
            # Requests go out little-endian, replies come back big-endian.
            mb = (header[3] << 8) | header[4]
            length = (header[8] << 8) | header[9]
            payload = await reader.readexactly(length) if length else b""
            self._handle_luci(mb, payload)

    def _handle_luci(self, mb: int, payload: bytes) -> None:
        text = payload.decode("utf-8", "replace").strip("\x00")
        changed = True
        if mb == MB_VOLUME and text.isdigit():
            self.state["volume"] = int(text)
        elif mb == MB_PLAY_STATUS and text.isdigit():
            self.state["play_state"] = PLAY_STATUS.get(int(text), text)
        elif mb == MB_SOURCE:
            self.state["source"] = text
        elif mb == MB_DEVICE_NAME:
            self.info["name"] = text
        elif mb == MB_MAC and (match := MAC_RE.search(text)):
            self.info["mac"] = match.group(1).upper()
        elif mb == MB_FIRMWARE:
            self.info["firmware"] = text
        elif mb == MB_ENV and text.startswith("Model:"):
            self.info["model"] = text.split(":", 1)[1]
        elif mb == MB_GET_UI and text.startswith("{"):
            self._handle_metadata(text)
        else:
            changed = False
        if changed:
            self._notify()

    def _handle_metadata(self, text: str) -> None:
        try:
            contents = json.loads(text).get("Window CONTENTS", {})
        except ValueError:
            return
        self.state["title"] = contents.get("TrackName") or None
        self.state["artist"] = contents.get("Artist") or None
        self.state["album"] = contents.get("Album") or None
        self.state["cover"] = contents.get("CoverArtUrl") or None
        if "PlayState" in contents:
            self.state["play_state"] = PLAY_STATUS.get(contents["PlayState"], contents["PlayState"])

    async def _read_tunnel(self, reader: asyncio.StreamReader) -> None:
        while True:
            length = int.from_bytes(await reader.readexactly(2), "big")
            body = await reader.readexactly(length)
            if length < 3:
                continue
            name = _BY_COMMAND.get((body[1], body[2]))
            data = body[3:]
            if not name or not data:
                continue
            kind = FIELDS[name][2]
            byte = data[0]
            if kind == EQ:
                value = decode_eq(byte)
            elif kind == BOOL:
                value = bool(byte)
            elif name == "preset":
                value = PRESETS[byte] if byte < len(PRESETS) else byte
            elif name == "output":
                value = OUTPUTS.get(byte, byte)
            else:
                value = byte
            if self.state.get(name) != value:
                self.state[name] = value
                self._notify()
