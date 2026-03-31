"""MQTT bridge — discovery, state publishing, and switch command handling."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any, Callable, Coroutine

import aiomqtt

_LOG = logging.getLogger(__name__)

SwitchCallback = Callable[[str, str, int, str], Coroutine[Any, Any, None]]
# (device_id, sub_device_id, outlet, "on"|"off")

# Maximum buffered messages when MQTT is disconnected
_BUFFER_MAX = 500


class MQTTBridge:
    """Manage MQTT connection, HA auto-discovery, and state publishing.

    Uses ``async with`` for the aiomqtt client lifecycle.  Call
    :meth:`connect` to establish (retries until successful), then
    :meth:`disconnect` when done.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
    ):
        self._host = host
        self._port = port
        self._username = username or None
        self._password = password or None
        self._client: aiomqtt.Client | None = None
        self._client_ctx: Any = None  # context manager instance
        self._switch_callback: SwitchCallback | None = None
        self._connected = False

        # Track which devices we've published discovery for.
        self._discovered: dict[str, dict] = {}

        # Buffer for state messages when MQTT is disconnected.
        self._buffer: deque[tuple[str, str, bool]] = deque(maxlen=_BUFFER_MAX)

        # The device_id used for LWT — set before connect().
        self._lwt_device_id: str | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def set_lwt_device(self, device_id: str) -> None:
        """Set the device ID for the Last Will and Testament message.

        Must be called before connect().
        """
        self._lwt_device_id = device_id

    async def connect(self) -> None:
        """Connect to the MQTT broker.  Retries until successful."""
        backoff = 5
        while True:
            try:
                will = None
                if self._lwt_device_id:
                    will = aiomqtt.Will(
                        topic=f"sonoff_spm/{self._lwt_device_id}/availability",
                        payload="offline",
                        retain=True,
                    )

                client = aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    will=will,
                )
                # Use the client as an async context manager
                self._client = await client.__aenter__()
                self._client_ctx = client
                self._connected = True
                _LOG.info("Connected to MQTT broker %s:%d", self._host, self._port)

                await self._flush_buffer()
                return
            except aiomqtt.MqttError as exc:
                _LOG.warning("MQTT connect failed (%s), retrying in %ds…", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def disconnect(self) -> None:
        if self._client_ctx:
            try:
                await self._client_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
            self._client_ctx = None
            self._connected = False
            _LOG.info("Disconnected from MQTT")

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def publish_discovery(
        self,
        device_id: str,
        device_name: str,
        sub_device_id: str,
        channels: list[int],
    ) -> None:
        """Publish HA MQTT discovery configs for all sensor types and switches.

        Each sub-device becomes its own HA device (model SPM-4Relay),
        linked to the main SPM device via ``via_device``.
        """
        # Parent device (SPM-Main) — referenced by via_device
        main_device_id = f"spm_{device_id}"

        # Sub-device as its own HA device
        sub_short = sub_device_id[-6:] if len(sub_device_id) > 6 else sub_device_id
        device_block = {
            "identifiers": [f"spm_{device_id}_{sub_device_id}"],
            "name": f"SPM Module {sub_short}",
            "manufacturer": "Sonoff",
            "model": "SPM-4Relay",
            "via_device": main_device_id,
        }

        base_topic = f"sonoff_spm/{device_id}/{sub_device_id}"
        avail_topic = f"sonoff_spm/{device_id}/availability"

        configs: list[tuple[str, dict]] = []

        for ch in channels:
            uid_prefix = f"spm_{device_id}_{sub_device_id}_ch{ch}"
            state_prefix = f"{base_topic}/ch{ch}"

            # Power sensor
            configs.append((
                f"homeassistant/sensor/{uid_prefix}_power/config",
                {
                    "name": f"Ch{ch} Power",
                    "state_topic": f"{state_prefix}/power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_class": "measurement",
                    "unique_id": f"{uid_prefix}_power",
                    "availability_topic": avail_topic,
                    "device": device_block,
                },
            ))

            # Voltage sensor
            configs.append((
                f"homeassistant/sensor/{uid_prefix}_voltage/config",
                {
                    "name": f"Ch{ch} Voltage",
                    "state_topic": f"{state_prefix}/voltage",
                    "device_class": "voltage",
                    "unit_of_measurement": "V",
                    "state_class": "measurement",
                    "unique_id": f"{uid_prefix}_voltage",
                    "availability_topic": avail_topic,
                    "device": device_block,
                },
            ))

            # Current sensor
            configs.append((
                f"homeassistant/sensor/{uid_prefix}_current/config",
                {
                    "name": f"Ch{ch} Current",
                    "state_topic": f"{state_prefix}/current",
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "state_class": "measurement",
                    "unique_id": f"{uid_prefix}_current",
                    "availability_topic": avail_topic,
                    "device": device_block,
                },
            ))

            # Energy sensor
            configs.append((
                f"homeassistant/sensor/{uid_prefix}_energy/config",
                {
                    "name": f"Ch{ch} Energy",
                    "state_topic": f"{state_prefix}/energy",
                    "device_class": "energy",
                    "unit_of_measurement": "kWh",
                    "state_class": "total_increasing",
                    "unique_id": f"{uid_prefix}_energy",
                    "availability_topic": avail_topic,
                    "device": device_block,
                },
            ))

            # Switch
            configs.append((
                f"homeassistant/switch/{uid_prefix}_switch/config",
                {
                    "name": f"Ch{ch}",
                    "state_topic": f"{state_prefix}/switch/state",
                    "command_topic": f"{state_prefix}/switch/set",
                    "unique_id": f"{uid_prefix}_switch",
                    "availability_topic": avail_topic,
                    "device": device_block,
                },
            ))

        # Publish a connectivity sensor on the main device so via_device resolves
        main_device_block = {
            "identifiers": [main_device_id],
            "name": f"Sonoff SPM {device_name}",
            "manufacturer": "Sonoff",
            "model": "SPM-Main",
        }
        configs.append((
            f"homeassistant/binary_sensor/spm_{device_id}_status/config",
            {
                "name": "Status",
                "state_topic": avail_topic,
                "payload_on": "online",
                "payload_off": "offline",
                "device_class": "connectivity",
                "unique_id": f"spm_{device_id}_status",
                "entity_category": "diagnostic",
                "device": main_device_block,
            },
        ))

        for topic, payload in configs:
            await self._publish(topic, json.dumps(payload), retain=True)

        self._discovered[f"{device_id}_{sub_device_id}"] = {
            "name": device_name,
            "sub_device_id": sub_device_id,
            "channels": channels,
        }
        _LOG.info(
            "Published MQTT discovery for %s/%s (%d channels, %d entities)",
            device_id, sub_device_id, len(channels), len(configs),
        )

    # ------------------------------------------------------------------
    # State publishing
    # ------------------------------------------------------------------

    async def publish_state(
        self,
        device_id: str,
        sub_id: str,
        channel: int,
        data: dict,
    ) -> None:
        """Publish all state values for one channel.

        *data* must contain keys: power, voltage, current, energy_kwh,
        switch_state.
        """
        prefix = f"sonoff_spm/{device_id}/{sub_id}/ch{channel}"
        await asyncio.gather(
            self._publish(f"{prefix}/power", f"{data['power']:.1f}"),
            self._publish(f"{prefix}/voltage", f"{data['voltage']:.2f}"),
            self._publish(f"{prefix}/current", f"{data['current']:.2f}"),
            self._publish(f"{prefix}/energy", f"{data['energy_kwh']:.3f}"),
            self._publish(f"{prefix}/switch/state", data["switch_state"].upper()),
        )

    async def publish_switch_only(
        self, device_id: str, sub_device_id: str, channel: int, switch_state: str,
    ) -> None:
        """Publish only the switch state for one channel."""
        topic = f"sonoff_spm/{device_id}/{sub_device_id}/ch{channel}/switch/state"
        await self._publish(topic, switch_state.upper())

    async def publish_availability(self, device_id: str, state: str) -> None:
        """Publish 'online' or 'offline' for the device."""
        await self._publish(
            f"sonoff_spm/{device_id}/availability", state, retain=True,
        )

    # ------------------------------------------------------------------
    # Switch commands (subscribe)
    # ------------------------------------------------------------------

    async def subscribe_switches(self, device_id: str, callback: SwitchCallback) -> None:
        """Subscribe to switch command topics and invoke *callback* on messages."""
        self._switch_callback = callback
        topic = f"sonoff_spm/{device_id}/+/+/switch/set"
        await self._client.subscribe(topic)
        _LOG.info("Subscribed to switch commands: %s", topic)

    async def listen_loop(self) -> None:
        """Block forever, dispatching incoming MQTT messages."""
        try:
            async for msg in self._client.messages:
                await self._on_message(msg)
        except aiomqtt.MqttError as exc:
            _LOG.error("MQTT connection lost: %s", exc)
            self._connected = False

    async def _on_message(self, msg: aiomqtt.Message) -> None:
        topic = str(msg.topic)
        payload = msg.payload.decode() if isinstance(msg.payload, bytes) else str(msg.payload)

        # Expected: sonoff_spm/{device_id}/{sub_device_id}/ch{N}/switch/set
        parts = topic.split("/")
        if len(parts) == 6 and parts[4] == "switch" and parts[5] == "set":
            device_id = parts[1]
            sub_device_id = parts[2]
            try:
                channel = int(parts[3].removeprefix("ch"))
            except ValueError:
                _LOG.warning("Bad switch topic: %s", topic)
                return

            state = payload.strip().lower()
            if state not in ("on", "off"):
                _LOG.warning("Bad switch payload: %s", payload)
                return

            if self._switch_callback:
                try:
                    await self._switch_callback(device_id, sub_device_id, channel, state)
                except Exception:
                    _LOG.exception("Switch callback error")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if not self._client or not self._connected:
            self._buffer.append((topic, payload, retain))
            return
        try:
            await self._client.publish(topic, payload, retain=retain)
        except aiomqtt.MqttError as exc:
            _LOG.error("MQTT publish to %s failed: %s", topic, exc)
            self._buffer.append((topic, payload, retain))

    async def _flush_buffer(self) -> None:
        """Publish all buffered messages after reconnecting."""
        if not self._buffer:
            return
        count = len(self._buffer)
        _LOG.info("Flushing %d buffered MQTT messages", count)
        while self._buffer:
            topic, payload, retain = self._buffer.popleft()
            try:
                await self._client.publish(topic, payload, retain=retain)
            except aiomqtt.MqttError as exc:
                _LOG.error("Failed to flush buffered message to %s: %s", topic, exc)
                break
