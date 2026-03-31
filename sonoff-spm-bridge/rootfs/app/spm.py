"""SPM client — HTTP API calls to the Sonoff SPM and webhook receiver."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

import aiohttp
from aiohttp import web

_LOG = logging.getLogger(__name__)

# SPM fixed API port
_SPM_PORT = 8081
_TIMEOUT = aiohttp.ClientTimeout(connect=10, total=30)


# ------------------------------------------------------------------
# Data types
# ------------------------------------------------------------------

class ChannelData:
    """Normalised reading for one outlet channel."""

    __slots__ = ("outlet", "switch", "power", "voltage", "current")

    def __init__(self, outlet: int, switch: str | None, power: float, voltage: float, current: float):
        self.outlet = outlet
        self.switch = switch      # "on" | "off" | None (unknown)
        self.power = power        # W
        self.voltage = voltage    # V
        self.current = current    # A

    def __repr__(self) -> str:
        return (f"Ch{self.outlet}({self.switch} {self.power}W "
                f"{self.voltage}V {self.current}A)")


class SubDeviceData:
    """Parsed response containing channel readings for one sub-device."""

    __slots__ = ("sub_device_id", "channels")

    def __init__(self, sub_device_id: str, channels: list[ChannelData]):
        self.sub_device_id = sub_device_id
        self.channels = channels


# Type alias for the callback that main.py wires up.
DataCallback = Callable[[SubDeviceData], Coroutine[Any, Any, None]]


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

def _parse_webhook_payload(data: dict) -> SubDeviceData | None:
    """Parse a webhook push payload (per-outlet format).

    Webhook pushes use flat keys without ``_XX`` suffix::

        {"subDevId": "abc", "outlet": 0, "current": 100,
         "voltage": 22000, "actPow": 15000}

    All electrical values are in 0.01 units (÷100).
    """
    sub_id = str(data.get("subDevId", ""))
    if "outlet" not in data:
        return None

    outlet = int(data["outlet"])
    channel = ChannelData(
        outlet=outlet,
        switch=None,  # webhook pushes don't include switch state
        power=float(data.get("actPow", 0)) / 100.0,
        voltage=float(data.get("voltage", 0)) / 100.0,
        current=float(data.get("current", 0)) / 100.0,
    )
    return SubDeviceData(sub_device_id=sub_id, channels=[channel])


def _parse_multi_channel(data: dict, sub_device_id: str = "") -> SubDeviceData | None:
    """Parse a multi-channel response (``_XX`` suffix format).

    Used by mDNS broadcasts and some firmware responses::

        {"switches": [...], "current_00": 100, "voltage_00": 22000,
         "actPow_00": 15000, ...}

    All electrical values are in 0.01 units (÷100).
    """
    switches = {s["outlet"]: s["switch"] for s in data.get("switches", [])}
    channels: list[ChannelData] = []

    for i in range(32):
        suffix = f"_{i:02d}"
        power_key = f"actPow{suffix}"
        if power_key not in data:
            break
        channels.append(ChannelData(
            outlet=i,
            switch=switches.get(i, "off"),
            power=float(data.get(power_key, 0)) / 100.0,
            voltage=float(data.get(f"voltage{suffix}", 0)) / 100.0,
            current=float(data.get(f"current{suffix}", 0)) / 100.0,
        ))

    if not channels:
        return None
    return SubDeviceData(sub_device_id=sub_device_id, channels=channels)


def _parse_data(body: dict) -> SubDeviceData | None:
    """Auto-detect payload format and parse accordingly."""
    data = body.get("data", body)
    device_id = str(body.get("deviceid", ""))

    # Webhook per-outlet format: has "outlet" key at top level of data
    if "outlet" in data:
        return _parse_webhook_payload(data)

    # Multi-channel _XX suffix format
    return _parse_multi_channel(data, sub_device_id=device_id)


# ------------------------------------------------------------------
# SPM HTTP client
# ------------------------------------------------------------------

class SPMClient:
    """Communicate with a Sonoff SPM device over its DIY-mode HTTP API.

    Also hosts the webhook server and polling loop, keeping all SPM I/O
    in one place.
    """

    def __init__(self, host: str):
        self._base = f"http://{host}:{_SPM_PORT}"
        self._session: aiohttp.ClientSession | None = None
        self._consecutive_failures = 0
        self._webhook_runner: web.AppRunner | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # -- API calls -------------------------------------------------

    async def discover(self) -> dict | None:
        """Call ``/zeroconf/deviceid`` and return device info dict, or None."""
        resp = await self._post("/zeroconf/deviceid", {"data": {}})
        if resp and resp.get("error") == 0:
            self._consecutive_failures = 0
            return resp.get("data", {})
        return None

    async def get_sub_devices(self, device_id: str) -> list[str]:
        """Return list of sub-device IDs via ``/zeroconf/subDevList``."""
        resp = await self._post("/zeroconf/subDevList", {
            "deviceid": device_id,
            "data": {},
        })
        if resp and resp.get("error") == 0:
            sub_list = resp.get("data", {}).get("subDevList", [])
            # subDevList may be a list of dicts with "subDevId" or a list of strings
            ids = []
            for item in sub_list:
                if isinstance(item, dict):
                    ids.append(str(item.get("subDevId", "")))
                else:
                    ids.append(str(item))
            return [i for i in ids if i]
        return []

    async def set_switch(self, device_id: str, sub_device_id: str, outlet: int, state: str) -> bool:
        """Turn an outlet on or off.  *state* must be ``"on"`` or ``"off"``."""
        resp = await self._post("/zeroconf/switches", {
            "deviceid": device_id,
            "data": {
                "subDevId": sub_device_id,
                "switches": [{"outlet": outlet, "switch": state}],
            },
        })
        ok = resp is not None and resp.get("error") == 0
        if ok:
            _LOG.info("Switch %s ch%d -> %s", sub_device_id, outlet, state)
        else:
            _LOG.error("Switch %s ch%d -> %s FAILED: %s", sub_device_id, outlet, state, resp)
        return ok

    async def get_switch_states(
        self, device_id: str, sub_device_ids: list[str],
    ) -> dict[str, dict[int, str]]:
        """Query ``/zeroconf/getState`` per sub-device for switch positions.

        Returns ``{sub_device_id: {outlet: "on"|"off", ...}, ...}``.
        """
        result: dict[str, dict[int, str]] = {}
        for sub_id in sub_device_ids:
            resp = await self._post("/zeroconf/getState", {
                "deviceid": device_id,
                "data": {"subDevId": sub_id},
            })
            if resp and resp.get("error") == 0:
                switches: dict[int, str] = {}
                for sw in resp.get("data", {}).get("switches", []):
                    switches[sw["outlet"]] = sw["switch"]
                if switches:
                    result[sub_id] = switches
        return result

    async def register_webhook(
        self,
        device_id: str,
        sub_device_id: str,
        addon_ip: str,
        port: int,
        outlets: list[int] | None = None,
        duration: int = 300,
    ) -> bool:
        """Ask SPM to push monitor data to our webhook server.

        Registers each *outlet* individually.  *duration* is how many
        seconds the SPM will keep pushing before the registration expires
        (max 3600).  Must be re-registered before it expires.
        """
        if outlets is None:
            outlets = [0, 1, 2, 3]
        all_ok = True
        for outlet in outlets:
            resp = await self._post("/zeroconf/monitor", {
                "deviceid": device_id,
                "data": {
                    "url": f"http://{addon_ip}",
                    "port": port,
                    "subDevId": sub_device_id,
                    "outlet": outlet,
                    "time": duration,
                },
            })
            ok = resp is not None and resp.get("error") == 0
            if not ok:
                _LOG.warning("Webhook registration failed for %s outlet %d: %s",
                             sub_device_id, outlet, resp)
                all_ok = False
        if all_ok:
            _LOG.info("Webhook registered for %s outlets %s (duration %ds)",
                      sub_device_id, outlets, duration)
        return all_ok

    # -- Webhook server --------------------------------------------

    async def start_webhook_server(self, port: int, callback: DataCallback) -> None:
        """Start an aiohttp server on ``0.0.0.0:{port}`` that receives
        data pushes from SPM and invokes *callback* for each."""
        async def handle(request: web.Request) -> web.Response:
            try:
                body = await request.json(content_type=None)
            except Exception:
                _LOG.warning("Webhook received non-JSON payload")
                return web.Response(status=400, text="bad request")

            result = _parse_data(body)
            if not result or not result.channels:
                _LOG.debug("Webhook payload contained no channel data")
                return web.Response(text="ok")

            try:
                await callback(result)
            except Exception:
                _LOG.exception("Error in webhook data callback")

            return web.Response(text="ok")

        app = web.Application()
        app.router.add_post("/{path:.*}", handle)
        self._webhook_runner = web.AppRunner(app)
        await self._webhook_runner.setup()
        site = web.TCPSite(self._webhook_runner, "0.0.0.0", port)
        await site.start()
        _LOG.info("Webhook server listening on port %d", port)

    async def stop_webhook_server(self) -> None:
        """Stop the webhook server if running."""
        if self._webhook_runner:
            await self._webhook_runner.cleanup()

    # -- Polling ---------------------------------------------------

    async def poll_loop(
        self,
        device_id: str,
        sub_device_ids: list[str],
        addon_ip: str,
        port: int,
        interval: int = 60,
        channels: list[int] | None = None,
    ) -> None:
        """Periodically re-register webhooks as a keep-alive.

        The SPM has no documented polling endpoint for current readings.
        Data arrives exclusively via webhook pushes.  This loop ensures
        the webhook registration stays active by re-registering before
        the monitoring duration expires.

        *interval* is how often we re-register (seconds).  The monitoring
        duration sent to the SPM is ``interval * 3`` to provide overlap.
        """
        duration = min(interval * 3, 3600)
        while True:
            await asyncio.sleep(interval)
            for sub_id in sub_device_ids:
                await self.register_webhook(
                    device_id, sub_id, addon_ip, port,
                    outlets=channels, duration=duration,
                )

    # -- Internal --------------------------------------------------

    async def _post(self, path: str, payload: dict) -> dict | None:
        try:
            session = await self._ensure_session()
            async with session.post(f"{self._base}{path}", json=payload) as resp:
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._consecutive_failures += 1
            level = logging.ERROR if self._consecutive_failures >= 3 else logging.WARNING
            _LOG.log(level, "SPM request %s failed (%d in a row): %s",
                     path, self._consecutive_failures, exc)
            return None

    @property
    def is_failing(self) -> bool:
        return self._consecutive_failures >= 3
