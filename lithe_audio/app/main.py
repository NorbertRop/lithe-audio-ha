"""Lithe Audio add-on entry point: discovery, MQTT bridge and ingress web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path

import aiohttp
from aiohttp import web
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from lithe import FIELDS, OUTPUTS, PLAY_COMMANDS, PRESETS, LitheSpeaker
from mqtt_bridge import MqttBridge

OPTIONS_FILE = Path(os.environ.get("OPTIONS_FILE", "/data/options.json"))
STATIC = Path(__file__).parent / "static"
AIRPLAY = "_airplay._tcp.local."

_LOGGER = logging.getLogger("lithe_audio")


class App:
    def __init__(self, options: dict) -> None:
        self.options = options
        self.speakers: dict[str, LitheSpeaker] = {}
        self.bridge: MqttBridge | None = None

    def add_speaker(self, host: str) -> None:
        if host in self.speakers:
            return
        _LOGGER.info("Adding speaker %s", host)
        speaker = LitheSpeaker(host, self._on_update)
        self.speakers[host] = speaker
        speaker.start()

    def _on_update(self, speaker: LitheSpeaker) -> None:
        if self.bridge:
            self.bridge.schedule_publish(speaker)

    # --- discovery ------------------------------------------------------

    async def discover(self) -> None:
        zc = AsyncZeroconf()

        def on_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                asyncio.ensure_future(self._resolve(zc, service_type, name))

        AsyncServiceBrowser(zc.zeroconf, [AIRPLAY], handlers=[on_change])
        await asyncio.Event().wait()

    async def _resolve(self, zc: AsyncZeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zc.zeroconf, 3000):
            return
        if (info.properties or {}).get(b"manufacturer") != b"Lithe Audio":
            return
        addresses = info.parsed_addresses(version=IPVersion.V4Only)
        if addresses:
            self.add_speaker(addresses[0])

    # --- web ui ---------------------------------------------------------

    def web_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/speakers", self._list)
        app.router.add_post("/api/speakers/{id}/{key}", self._command)
        return app

    async def _index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    async def _list(self, request: web.Request) -> web.Response:
        return web.json_response({
            "speakers": [s.as_dict() for s in self.speakers.values()],
            "presets": PRESETS,
            "outputs": list(OUTPUTS.values()),
        })

    async def _command(self, request: web.Request) -> web.Response:
        speaker = next((s for s in self.speakers.values() if s.id == request.match_info["id"]), None)
        if not speaker or not speaker.available:
            raise web.HTTPNotFound(text="speaker not available")
        key = request.match_info["key"]
        value = (await request.json()).get("value")
        try:
            if key == "volume":
                speaker.set_volume(int(value))
            elif key in PLAY_COMMANDS:
                speaker.play_control(key)
            elif key in FIELDS:
                await speaker.set_field(key, value)
            else:
                raise web.HTTPBadRequest(text=f"unknown setting {key}")
        except (ValueError, KeyError, TypeError) as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return web.json_response({"ok": True})


async def _supervisor_mqtt() -> dict | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                if resp.status != 200:
                    return None
                return (await resp.json()).get("data")
    except aiohttp.ClientError:
        return None


async def main() -> None:
    options = json.loads(OPTIONS_FILE.read_text()) if OPTIONS_FILE.exists() else {}
    logging.basicConfig(
        level=options.get("log_level", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = App(options)
    tasks = []

    if options.get("mqtt_enabled", True):
        if options.get("mqtt_host"):
            mqtt = {
                "host": options["mqtt_host"],
                "port": options.get("mqtt_port", 1883),
                "username": options.get("mqtt_username"),
                "password": options.get("mqtt_password"),
            }
        else:
            mqtt = await _supervisor_mqtt() or {}
        if mqtt.get("host"):
            app.bridge = MqttBridge(
                app.speakers, mqtt["host"], int(mqtt.get("port", 1883)), mqtt.get("username"), mqtt.get("password")
            )
            tasks.append(app.bridge.run())
        else:
            _LOGGER.warning("No MQTT broker found; only the web UI will be available")

    for host in options.get("speakers", []):
        app.add_speaker(socket.gethostbyname(host))
    if options.get("discovery", True):
        tasks.append(app.discover())

    runner = web.AppRunner(app.web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("WEB_PORT", 8099))).start()
    _LOGGER.info("Web UI listening on port %s", os.environ.get("WEB_PORT", 8099))

    await asyncio.gather(*tasks, asyncio.Event().wait())


if __name__ == "__main__":
    asyncio.run(main())
