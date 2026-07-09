"""SPM client — HTTP API calls to the Sonoff SPM and webhook receiver."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import aiohttp

_LOG = logging.getLogger(__name__)

# SPM fixed API port
_SPM_PORT = 8081
_TIMEOUT = aiohttp.ClientTimeout(connect=10, total=30)

# The SPM is a small embedded device whose HTTP API returns {"error": 400}
# when hit with too many requests too quickly.  Pace webhook registrations
# and retry the transient rejections.
_MONITOR_RETRIES = 4         # attempts per outlet before giving up
_MONITOR_RETRY_DELAY = 1.0   # base backoff between retries (grows per attempt)

# The SPM only streams data for the most-recently-registered monitor: each
# /zeroconf/monitor registration evicts the previous one.  We therefore
# rotate a single active subscription across every channel, dwelling on
# each long enough to receive a few pushes before advancing.
_ROTATE_DWELL_SECONDS = 3.0


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
        self._webhook_server: asyncio.AbstractServer | None = None

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

    async def _register_one(
        self,
        device_id: str,
        sub_device_id: str,
        addon_ip: str,
        port: int,
        outlet: int,
        duration: int,
    ) -> bool:
        """Register a single outlet's webhook, retrying transient failures."""
        payload = {
            "deviceid": device_id,
            "data": {
                "url": f"http://{addon_ip}",
                "port": port,
                "subDevId": sub_device_id,
                "outlet": outlet,
                "time": duration,
            },
        }
        last_resp: dict | None = None
        for attempt in range(1, _MONITOR_RETRIES + 1):
            resp = await self._post("/zeroconf/monitor", payload)
            if resp is not None and resp.get("error") == 0:
                return True
            last_resp = resp
            if attempt < _MONITOR_RETRIES:
                await asyncio.sleep(_MONITOR_RETRY_DELAY * attempt)
        _LOG.warning(
            "Webhook registration failed for %s outlet %d after %d attempts: %s",
            sub_device_id, outlet, _MONITOR_RETRIES, last_resp,
        )
        return False

    # -- Webhook server --------------------------------------------

    async def start_webhook_server(self, port: int, callback: DataCallback) -> None:
        """Start a raw asyncio HTTP server on ``0.0.0.0:{port}``.

        The Sonoff SPM pushes data with a minimal, non-compliant HTTP/1.0
        request that omits the ``Host`` header, which aiohttp's strict
        parser rejects with a 400 before any handler runs.  This
        hand-rolled parser tolerates it: read the request, extract the
        JSON body, invoke *callback*, and always answer ``200 ok``.
        """
        async def handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        ) -> None:
            try:
                await self._handle_webhook_request(reader, writer, callback)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError):
                _LOG.debug("Webhook request incomplete or timed out")
            except Exception:
                _LOG.exception("Webhook request handling error")
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        self._webhook_server = await asyncio.start_server(
            handle_client, "0.0.0.0", port,
        )
        _LOG.info("Webhook server listening on port %d", port)

    @staticmethod
    async def _handle_webhook_request(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        callback: DataCallback,
    ) -> None:
        """Parse one HTTP request, dispatch its JSON body, and reply 200."""
        # Read headers (everything up to the blank line).
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)

        content_length = 0
        for line in head.split(b"\r\n")[1:]:
            name, sep, value = line.partition(b":")
            if sep and name.strip().lower() == b"content-length":
                try:
                    content_length = int(value.strip())
                except ValueError:
                    content_length = 0
                break

        # Read the body: use Content-Length if present, else until EOF.
        if content_length > 0:
            body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=10,
            )
        else:
            body = await asyncio.wait_for(reader.read(65536), timeout=10)

        if body:
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                _LOG.warning("Webhook received non-JSON payload")
                payload = None

            if isinstance(payload, dict):
                result = _parse_data(payload)
                if result and result.channels:
                    try:
                        await callback(result)
                    except Exception:
                        _LOG.exception("Error in webhook data callback")
                else:
                    _LOG.debug("Webhook payload contained no channel data")

        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"ok"
        )
        await writer.drain()

    async def stop_webhook_server(self) -> None:
        """Stop the webhook server if running."""
        if self._webhook_server:
            self._webhook_server.close()
            try:
                await self._webhook_server.wait_closed()
            except Exception:
                pass

    # -- Rotating subscription poller ------------------------------

    async def rotate_loop(
        self,
        device_id: str,
        sub_device_ids: list[str],
        addon_ip: str,
        port: int,
        channels: list[int] | None = None,
        dwell: float = _ROTATE_DWELL_SECONDS,
    ) -> None:
        """Rotate a single active monitor subscription across all channels.

        The SPM has no polling endpoint for readings — data arrives only via
        webhook pushes — and it streams only the most-recently-registered
        monitor (a new registration evicts the previous one).  So we register
        one ``(sub-device, outlet)`` target at a time, dwell briefly to
        receive its pushes, then advance to the next.  One full pass refreshes
        every channel; the cycle then repeats forever.
        """
        if channels is None:
            channels = [0, 1, 2, 3]
        targets = [(sub, outlet) for sub in sub_device_ids for outlet in channels]
        if not targets:
            _LOG.warning("No monitor targets to rotate")
            return

        # Registration must outlive the dwell so it doesn't self-expire before
        # the next target replaces it.
        duration = max(int(dwell * 3), 15)
        _LOG.info(
            "Rotating monitor subscription across %d target(s), ~%.0fs per cycle",
            len(targets), dwell * len(targets),
        )
        idx = 0
        while True:
            sub_id, outlet = targets[idx]
            await self._register_one(
                device_id, sub_id, addon_ip, port, outlet, duration,
            )
            await asyncio.sleep(dwell)
            idx = (idx + 1) % len(targets)

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
