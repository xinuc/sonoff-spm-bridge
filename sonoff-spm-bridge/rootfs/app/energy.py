"""Energy tracker — accumulates instantaneous power into cumulative kWh."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger(__name__)

# If more than this many seconds pass between readings, assume the device
# was offline and skip the gap instead of accumulating a huge phantom delta.
_MAX_GAP_SECONDS = 600


class EnergyTracker:
    """Track cumulative energy (kWh) per channel from instantaneous power readings.

    Uses trapezoidal integration: averages consecutive power readings and
    multiplies by elapsed time.  Persists state to a JSON file so totals
    survive addon restarts.

    Only ``kwh`` and ``last_update`` are persisted to disk (matching the
    design schema).  ``last_power_w`` is kept in memory only — after a
    restart the first reading establishes a new baseline without adding
    energy.
    """

    def __init__(self, persist_path: str = "/data/energy_state.json"):
        self._path = Path(persist_path)
        # Persisted state: channel_key -> {"kwh": float, "last_update": str}
        self._state: dict[str, dict] = {}
        # In-memory only: channel_key -> last power reading (watts)
        self._last_power: dict[str, float] = {}
        self.load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, channel_key: str, power_w: float, timestamp: datetime | None = None) -> float:
        """Record a power reading and return the updated cumulative kWh.

        Args:
            channel_key: Unique identifier like ``sub1_ch0``.
            power_w: Instantaneous power in watts.
            timestamp: Reading time (default: now UTC).

        Returns:
            Cumulative energy in kWh for this channel.
        """
        now = timestamp or datetime.now(timezone.utc)

        if channel_key not in self._state:
            self._state[channel_key] = {
                "kwh": 0.0,
                "last_update": now.isoformat(),
            }
            self._last_power[channel_key] = power_w
            return 0.0

        entry = self._state[channel_key]
        last_update = datetime.fromisoformat(entry["last_update"])
        elapsed = (now - last_update).total_seconds()

        if elapsed <= 0:
            return entry["kwh"]

        # Only accumulate energy when: gap is reasonable, current power > 0,
        # and we have a previous power reading in memory.
        if (
            elapsed <= _MAX_GAP_SECONDS
            and power_w > 0
            and channel_key in self._last_power
        ):
            avg_power = (self._last_power[channel_key] + power_w) / 2.0
            delta_kwh = avg_power * elapsed / 3_600_000.0
            entry["kwh"] += delta_kwh

        self._last_power[channel_key] = power_w
        entry["last_update"] = now.isoformat()
        return entry["kwh"]

    def get_total(self, channel_key: str) -> float:
        """Return current cumulative kWh for *channel_key* (0.0 if unseen)."""
        entry = self._state.get(channel_key)
        return entry["kwh"] if entry else 0.0

    def save(self) -> None:
        """Flush state to disk (only kwh + last_update per channel)."""
        try:
            self._path.write_text(json.dumps(self._state, indent=2))
            _LOG.debug("Energy state saved (%d channels)", len(self._state))
        except OSError:
            _LOG.exception("Failed to save energy state")

    def load(self) -> None:
        """Load persisted state from disk."""
        if not self._path.exists():
            _LOG.info("No persisted energy state — starting fresh")
            return
        try:
            self._state = json.loads(self._path.read_text())
            _LOG.info("Loaded energy state for %d channels", len(self._state))
        except (json.JSONDecodeError, OSError):
            _LOG.exception("Corrupt energy state file — starting fresh")
            self._state = {}

    async def persist_loop(self, interval: int = 300) -> None:
        """Flush energy state to disk every *interval* seconds.

        Designed to run as a background task — loops until cancelled.
        """
        while True:
            await asyncio.sleep(interval)
            self.save()
