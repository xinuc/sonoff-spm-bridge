"""Sonoff SPM to MQTT Bridge — main entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import socket
import sys
from pathlib import Path

from energy import EnergyTracker
from mqtt import MQTTBridge
from spm import SPMClient, SubDeviceData

_LOG = logging.getLogger("spm_bridge")

# HA addon config path
_OPTIONS_PATH = "/data/options.json"

# Default channels per SPM-4Relay module
_DEFAULT_CHANNELS = [0, 1, 2, 3]


def _load_config() -> dict:
    """Load addon options written by the HA Supervisor."""
    path = Path(_OPTIONS_PATH)
    if not path.exists():
        _LOG.error("Options file not found at %s", _OPTIONS_PATH)
        sys.exit(1)
    with path.open() as f:
        return json.load(f)


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _get_local_ip(target_host: str) -> str | None:
    """Determine our own IP address reachable from *target_host*."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target_host, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


class SPMBridge:
    """Top-level orchestrator wiring SPM, MQTT, and energy tracking together."""

    def __init__(self, config: dict):
        self._config = config
        self._spm = SPMClient(config["spm_host"])
        self._mqtt = MQTTBridge(
            host=config["mqtt_host"],
            port=config.get("mqtt_port", 1883),
            username=config.get("mqtt_username", ""),
            password=config.get("mqtt_password", ""),
        )
        self._energy = EnergyTracker()
        self._device_id: str | None = None
        self._device_name: str | None = None
        self._sub_device_ids: list[str] = []
        self._addon_ip: str | None = None
        self._shutdown_event = asyncio.Event()
        # Track last known switch state per channel (webhook pushes don't include it)
        self._switch_states: dict[str, str] = {}  # "subid_chN" -> "on"|"off"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start all components and block until shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown)

        # 1-2. Config + EnergyTracker already initialised in __init__

        # 3. Connect to MQTT broker (initially without LWT)
        await self._mqtt.connect()

        # 4. Discover SPM device
        device_info = await self._discover_spm()
        self._device_id = device_info.get("deviceid", "")
        self._device_name = self._device_id

        # Reconnect MQTT with proper LWT now that we know device_id
        self._mqtt.set_lwt_device(self._device_id)
        await self._mqtt.disconnect()
        await self._mqtt.connect()

        # 5. Discover sub-devices
        self._sub_device_ids = await self._spm.get_sub_devices(self._device_id)
        if not self._sub_device_ids:
            _LOG.warning("No sub-devices found — using device_id as sub-device")
            self._sub_device_ids = [self._device_id]

        _LOG.info("Found %d sub-device(s): %s",
                  len(self._sub_device_ids), self._sub_device_ids)

        # 6. Publish MQTT discovery for each sub-device (assume 4 channels each)
        for sub_id in self._sub_device_ids:
            await self._mqtt.publish_discovery(
                device_id=self._device_id,
                device_name=self._device_name,
                sub_device_id=sub_id,
                channels=_DEFAULT_CHANNELS,
            )

        # 7. Query initial switch states
        await self._fetch_initial_switch_states()

        # 8. Determine our IP and register webhooks (all outlets per sub-device)
        poll_interval = self._config.get("poll_interval", 60)
        webhook_duration = min(poll_interval * 3, 3600)
        self._addon_ip = _get_local_ip(self._config["spm_host"])
        if self._addon_ip:
            webhook_port = self._config.get("webhook_port", 8080)
            for sub_id in self._sub_device_ids:
                await self._spm.register_webhook(
                    self._device_id, sub_id, self._addon_ip, webhook_port,
                    outlets=_DEFAULT_CHANNELS, duration=webhook_duration,
                )
        else:
            _LOG.warning("Could not determine addon IP — webhook registration skipped")

        # 9. Start webhook server
        await self._spm.start_webhook_server(
            port=self._config.get("webhook_port", 8080),
            callback=self._on_spm_data,
        )

        # 10-11. Subscribe to switch commands
        await self._mqtt.subscribe_switches(self._device_id, self._on_switch_command)

        # 12. Mark online
        await self._mqtt.publish_availability(self._device_id, "online")

        # 13. Start background tasks
        _LOG.info("SPM Bridge running — device %s, %d sub-device(s)",
                  self._device_id, len(self._sub_device_ids))

        tasks = [
            asyncio.create_task(self._energy.persist_loop(), name="energy_persist"),
            asyncio.create_task(self._discovery_refresh_loop(), name="discovery_refresh"),
            asyncio.create_task(self._mqtt_listen_task(), name="mqtt_listen"),
        ]

        # Webhook re-registration loop (keep-alive)
        if self._addon_ip:
            tasks.append(asyncio.create_task(
                self._spm.poll_loop(
                    device_id=self._device_id,
                    sub_device_ids=self._sub_device_ids,
                    addon_ip=self._addon_ip,
                    port=self._config.get("webhook_port", 8080),
                    interval=poll_interval,
                    channels=_DEFAULT_CHANNELS,
                ),
                name="webhook_keepalive",
            ))

        await self._shutdown_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Graceful shutdown.

        1. Publish availability "offline"
        2. Save energy state to disk
        3. Disconnect MQTT
        4. Stop webhook server
        5. Exit
        """
        _LOG.info("Shutting down…")
        if self._device_id:
            await self._mqtt.publish_availability(self._device_id, "offline")
        self._energy.save()
        await self._mqtt.disconnect()
        await self._spm.stop_webhook_server()
        await self._spm.close()
        _LOG.info("Shutdown complete")

    def _request_shutdown(self) -> None:
        _LOG.info("Shutdown signal received")
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # SPM discovery
    # ------------------------------------------------------------------

    async def _fetch_initial_switch_states(self) -> None:
        """Query SPM for current switch positions and publish them."""
        states = await self._spm.get_switch_states(self._device_id, self._sub_device_ids)
        for sub_id, switches in states.items():
            for outlet, state in switches.items():
                key = f"{sub_id}_ch{outlet}"
                self._switch_states[key] = state
                await self._mqtt.publish_switch_only(
                    self._device_id, sub_id, outlet, state,
                )
        if states:
            _LOG.info("Fetched initial switch states for %d sub-device(s)", len(states))
        else:
            _LOG.warning("Could not fetch initial switch states")

    async def _discover_spm(self) -> dict:
        """Keep trying to get the SPM device info until we succeed."""
        backoff = 30
        while True:
            info = await self._spm.discover()
            if info:
                _LOG.info("Discovered SPM device: %s", info.get("deviceid"))
                return info
            _LOG.warning("SPM not reachable at %s, retrying in %ds…",
                         self._config["spm_host"], backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    # ------------------------------------------------------------------
    # Data handling
    # ------------------------------------------------------------------

    async def _on_spm_data(self, data: SubDeviceData) -> None:
        """Process incoming SPM data from webhook pushes."""
        for ch in data.channels:
            channel_key = f"{data.sub_device_id}_ch{ch.outlet}"
            energy_kwh = self._energy.update(channel_key, ch.power)

            # Webhook pushes don't include switch state — use last known
            if ch.switch is not None:
                self._switch_states[channel_key] = ch.switch
            switch_state = self._switch_states.get(channel_key, "off")

            await self._mqtt.publish_state(
                device_id=self._device_id,
                sub_id=data.sub_device_id,
                channel=ch.outlet,
                data={
                    "power": ch.power,
                    "voltage": ch.voltage,
                    "current": ch.current,
                    "energy_kwh": energy_kwh,
                    "switch_state": switch_state,
                },
            )

        if not self._spm.is_failing:
            await self._mqtt.publish_availability(self._device_id, "online")

    async def _on_switch_command(
        self, device_id: str, sub_device_id: str, outlet: int, state: str,
    ) -> None:
        """Handle switch command from HA via MQTT."""
        ok = await self._spm.set_switch(device_id, sub_device_id, outlet, state)
        if ok:
            self._switch_states[f"{sub_device_id}_ch{outlet}"] = state
            await self._mqtt.publish_switch_only(device_id, sub_device_id, outlet, state)

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _discovery_refresh_loop(self) -> None:
        """Re-publish MQTT discovery every 30 minutes."""
        while True:
            await asyncio.sleep(1800)
            if not (self._device_id and self._mqtt.connected):
                continue
            for sub_id in self._sub_device_ids:
                await self._mqtt.publish_discovery(
                    self._device_id, self._device_name, sub_id, _DEFAULT_CHANNELS,
                )

    async def _mqtt_listen_task(self) -> None:
        """Run the MQTT message listener.  Reconnects on failure."""
        while True:
            try:
                await self._mqtt.listen_loop()
            except Exception:
                _LOG.exception("MQTT listener crashed")
            _LOG.warning("Reconnecting MQTT in 5s…")
            await asyncio.sleep(5)
            await self._mqtt.disconnect()
            await self._mqtt.connect()
            if self._device_id:
                await self._mqtt.subscribe_switches(
                    self._device_id, self._on_switch_command,
                )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

async def _main() -> None:
    config = _load_config()
    _setup_logging(config.get("log_level", "info"))

    if not config.get("spm_host"):
        _LOG.error("spm_host is not configured — set it in the addon settings")
        sys.exit(1)

    bridge = SPMBridge(config)
    try:
        await bridge.run()
    finally:
        await bridge.shutdown()


if __name__ == "__main__":
    asyncio.run(_main())
