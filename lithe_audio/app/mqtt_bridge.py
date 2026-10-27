"""Expose speakers to Home Assistant through MQTT discovery."""

from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt

from lithe import FIELDS, OUTPUTS, PLAY_COMMANDS, PRESETS, LitheSpeaker

_LOGGER = logging.getLogger(__name__)

BASE = "lithe_audio"
DISCOVERY = "homeassistant"

EQ_ICONS = {"treble": "mdi:equalizer", "mid": "mdi:equalizer", "bass": "mdi:speaker", "balance": "mdi:pan-horizontal"}
SWITCH_ICONS = {
    "loudness": "mdi:volume-plus",
    "night_mode": "mdi:weather-night",
    "high_pass_filter": "mdi:filter",
    "bluetooth": "mdi:bluetooth",
    "aux_in": "mdi:audio-input-rca",
}
BUTTON_ICONS = {"play": "mdi:play", "pause": "mdi:pause", "next": "mdi:skip-next", "previous": "mdi:skip-previous"}


def _title(key: str) -> str:
    return key.replace("_", " ").capitalize()


class MqttBridge:
    def __init__(self, speakers: dict[str, LitheSpeaker], host: str, port: int, username: str | None, password: str | None):
        self._speakers = speakers
        self._params = {"hostname": host, "port": port, "username": username or None, "password": password or None}
        self._client: aiomqtt.Client | None = None
        self._announced: dict[str, set[str]] = {}

    async def run(self) -> None:
        while True:
            try:
                async with aiomqtt.Client(**self._params, identifier="lithe-audio-addon") as client:
                    self._client = client
                    self._announced.clear()
                    _LOGGER.info("MQTT connected to %s", self._params["hostname"])
                    await client.subscribe(f"{BASE}/+/+/set")
                    await client.subscribe(f"{DISCOVERY}/status")
                    for speaker in list(self._speakers.values()):
                        await self.publish(speaker)
                    async for message in client.messages:
                        await self._handle(message)
            except aiomqtt.MqttError as err:
                _LOGGER.warning("MQTT error: %s, reconnecting in 10s", err)
            self._client = None
            await asyncio.sleep(10)

    def schedule_publish(self, speaker: LitheSpeaker) -> None:
        if self._client:
            asyncio.get_running_loop().create_task(self.publish(speaker))

    async def publish(self, speaker: LitheSpeaker) -> None:
        client = self._client
        if not client or "mac" not in speaker.info:
            return
        announced = self._announced.setdefault(speaker.id, set())
        for config_topic, config in self._entities(speaker):
            if config_topic not in announced:
                await client.publish(config_topic, json.dumps(config), retain=True)
                announced.add(config_topic)
        topic = f"{BASE}/{speaker.id}"
        await client.publish(f"{topic}/availability", "online" if speaker.available else "offline", retain=True)
        await client.publish(f"{topic}/state", json.dumps(speaker.as_dict()), retain=True)

    def _entities(self, speaker: LitheSpeaker) -> list[tuple[str, dict]]:
        sid = speaker.id
        topic = f"{BASE}/{sid}"
        device = {
            "identifiers": [f"lithe_{sid}"],
            "connections": [["mac", speaker.info["mac"]]],
            "name": speaker.name,
            "manufacturer": "Lithe Audio",
            "model": speaker.info.get("model"),
            "sw_version": speaker.info.get("firmware"),
            "configuration_url": f"http://{speaker.host}/",
        }

        def base(key: str, component: str, **extra) -> tuple[str, dict]:
            config = {
                "name": _title(key),
                "unique_id": f"lithe_{sid}_{key}",
                "object_id": f"{speaker.name}_{key}",
                "device": device,
                "availability_topic": f"{topic}/availability",
                "state_topic": f"{topic}/state",
                **extra,
            }
            return f"{DISCOVERY}/{component}/lithe_{sid}/{key}/config", config

        entities = [
            base("volume", "number", min=0, max=100, step=1, mode="slider", icon="mdi:volume-high",
                 value_template="{{ value_json.volume }}", command_topic=f"{topic}/volume/set"),
            base("preset", "select", options=PRESETS, icon="mdi:tune-vertical",
                 value_template="{{ value_json.preset }}", command_topic=f"{topic}/preset/set"),
            base("output", "select", options=list(OUTPUTS.values()), icon="mdi:speaker-multiple",
                 value_template="{{ value_json.output }}", command_topic=f"{topic}/output/set",
                 entity_category="config"),
            base("play_state", "sensor", icon="mdi:play-pause", value_template="{{ value_json.play_state }}"),
            base("track", "sensor", icon="mdi:music",
                 value_template="{{ [value_json.artist, value_json.title] | select | join(' - ') or None }}"),
        ]
        for key, icon in EQ_ICONS.items():
            entities.append(base(key, "number", min=-6, max=6, step=1, mode="slider", icon=icon,
                                 value_template=f"{{{{ value_json.{key} }}}}", command_topic=f"{topic}/{key}/set"))
        for key, icon in SWITCH_ICONS.items():
            entities.append(base(key, "switch", icon=icon, payload_on="ON", payload_off="OFF",
                                 value_template=f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
                                 command_topic=f"{topic}/{key}/set",
                                 **({"entity_category": "config"} if key in ("bluetooth", "aux_in") else {})))
        for key, icon in BUTTON_ICONS.items():
            config_topic, config = base(key, "button", icon=icon, command_topic=f"{topic}/{key}/set", payload_press="PRESS")
            del config["state_topic"]
            entities.append((config_topic, config))

        # Only announce settings this model actually reports (e.g. v3 has no high-pass filter).
        return [(t, c) for t, c in entities if t.split("/")[-2] not in FIELDS or t.split("/")[-2] in speaker.state]

    async def _handle(self, message: aiomqtt.Message) -> None:
        topic = str(message.topic)
        payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
        if topic == f"{DISCOVERY}/status":
            if payload == "online":
                self._announced.clear()
                for speaker in list(self._speakers.values()):
                    await self.publish(speaker)
            return
        _, sid, key, _ = topic.split("/")
        speaker = next((s for s in self._speakers.values() if s.id == sid), None)
        if not speaker or not speaker.available:
            return
        try:
            if key == "volume":
                speaker.set_volume(int(float(payload)))
            elif key in PLAY_COMMANDS:
                speaker.play_control(key)
            elif key in FIELDS:
                value = int(float(payload)) if FIELDS[key][2] == "eq" else payload
                await speaker.set_field(key, value)
        except (ValueError, KeyError, ConnectionError) as err:
            _LOGGER.warning("%s: command %s=%s failed: %s", speaker.host, key, payload, err)
