"""HTTP client helpers for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional, Set

import aiohttp

from .const import (
    ALL_METRICS,
    CONF_DAYS_BACK,
    CONF_GRANULARITY,
    CONF_GAS_UNIT,
    CONF_TIMEZONE,
    CONF_VERBOSE_LOGGING,
    DEFAULT_DAYS_BACK,
    DEFAULT_GRANULARITY,
    DEFAULT_GAS_UNIT,
    DEFAULT_METER_TYPE,
    DEFAULT_TIMEZONE,
    DEFAULT_VERBOSE_LOGGING,
    GAS_DAY_START_HOUR,
    GAS_MIN_INTERVAL_LOOKBACK_DAYS,
    GAS_MIN_LOOKBACK_DAYS,
    GAS_UNIT_CUBIC_METERS,
    INTERVAL_EXTRA_PARAMS,
    INTERVAL_GRANULARITY_CANDIDATES,
    GAS_SUPPORTED_GRANULARITY,
    INTERVAL_MINUTES_BY_METER_TYPE,
    MAX_INTERVAL_DAYS_BACK,
    METER_TYPE_ELECTRICITY,
    METER_TYPE_GAS,
)
from .auth import FluviusAuthError, async_get_bearer_token

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Windows without tzdata
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore


LOGGER = logging.getLogger(__name__)

CUBIC_METER_UNIT_CODE = 5
KILO_WATT_HOUR_UNIT_CODE = 3


class FluviusApiError(RuntimeError):
    """Raised when the Fluvius API call fails."""


@dataclass(slots=True)
class FluviusDailySummary:
    """Container for a single day of energy data."""

    day_id: str
    start: datetime
    end: datetime
    metrics: Dict[str, float]


@dataclass(slots=True)
class FluviusPeakMeasurement:
    """Container describing the monthly peak power measurement."""

    period_start: datetime
    period_end: datetime
    spike_start: datetime
    spike_end: datetime
    value_kw: float


@dataclass(slots=True)
class FluviusIntervalMeasurement:
    """Container for one sub-daily interval: 15 minutes for electricity, 60 for gas."""

    start: datetime
    end: datetime
    consumption: float  # kWh consumed in this interval
    injection: float  # kWh injected in this interval


class FluviusApiClient:
    """Thin wrapper around the HTTP helpers used by the CLI script."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        ean: str,
        meter_serial: str,
        meter_type: str = DEFAULT_METER_TYPE,
        remember_me: bool = False,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._ean = ean
        self._meter_serial = meter_serial
        self._meter_type = meter_type
        self._remember_me = remember_me
        self._options = options or {}
        self._verbose = bool(self._options.get(CONF_VERBOSE_LOGGING, DEFAULT_VERBOSE_LOGGING))
        # Resolved on first successful fetch and sticky for the lifetime of the
        # client, so the probe costs at most a few extra requests once.
        self._interval_granularity: Optional[str] = None
        # What each probed granularity code answered, for a single diagnostic line.
        self._probe_outcomes: Dict[str, str] = {}
        # Set when a full probe found nothing, to stop re-asking every hour. Cleared
        # by reloading the entry, so a future Fluvius change is still picked up.
        self._interval_unavailable = False

    def _log_verbose(self, message: str, *args: Any) -> None:
        """Log a message only if verbose logging is enabled."""
        if self._verbose:
            LOGGER.debug("[VERBOSE] " + message, *args)

    @property
    def interval_minutes(self) -> int:
        """Native resolution of this meter: 15 for electricity, 60 for gas."""

        return self._expected_interval_minutes()

    @property
    def resolved_granularity(self) -> Optional[str]:
        """Granularity code the probe settled on, None while still unresolved."""

        return self._interval_granularity

    @property
    def probe_outcomes(self) -> Dict[str, str]:
        """What each probed granularity code answered, for diagnostics."""

        return dict(self._probe_outcomes)

    @property
    def interval_unavailable(self) -> bool:
        """True once a full probe established this meter has no sub-daily data."""

        return self._interval_unavailable

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def fetch_daily_summaries(self) -> List[FluviusDailySummary]:
        """Retrieve the most recent consumption data and return parsed summaries."""

        summaries, _ = await self._fetch_summaries_and_spikes(include_spikes=False)
        return summaries

    async def fetch_daily_summaries_with_spikes(self) -> tuple[
        List[FluviusDailySummary],
        List[FluviusPeakMeasurement],
    ]:
        """Return both the daily summaries and the monthly peak power values."""

        return await self._fetch_summaries_and_spikes(include_spikes=True)

    async def fetch_interval_consumption(
        self,
        days_back: Optional[int] = None,
        skip_hours: Optional[Set[datetime]] = None,
    ) -> List[FluviusIntervalMeasurement]:
        """Retrieve sub-daily consumption data at the meter's native resolution.

        Electricity is registered per quarter-hour, gas per hour. Fluvius only
        publishes this data for days that are already closed, so the window always
        ends yesterday, and it refuses multi-day ranges at these granularities,
        hence one request per day.

        Args:
            days_back: How many past days to cover. Defaults to the configured
                option, widened to a floor for gas because Fluvius releases gas
                measurements with roughly a 72-hour delay.
            skip_hours: UTC hours already held elsewhere. A day whose hours are all
                present is not requested at all, which in steady state cuts the whole
                window down to the single day that is actually new.

        Returns:
            List of interval measurements sorted by start time.
        """
        if self._interval_unavailable:
            LOGGER.debug(
                "Skipping interval fetch for meter %s: a full probe already established "
                "that Fluvius serves no sub-daily data here. Reload the entry to retry.",
                self._meter_serial,
            )
            return []

        window = self._resolve_interval_days_back(days_back)
        wanted = [
            day_offset
            for day_offset in range(window, 0, -1)
            if not self._day_already_held(day_offset, skip_hours)
        ]
        skipped_days = window - len(wanted)

        if not wanted:
            LOGGER.debug(
                "Interval data: all %d day(s) of the window are already imported, "
                "nothing to fetch",
                window,
            )
            return []

        # Only authenticate once we know there is something to ask for.
        access_token = await self._async_get_access_token()

        measurements: List[FluviusIntervalMeasurement] = []
        seen_starts: set[datetime] = set()
        # Oldest first, so the result is already ordered and the granularity probe
        # hits the day most likely to hold data.
        for day_offset in wanted:
            day_measurements = await self._async_fetch_interval_day(access_token, day_offset)
            for item in day_measurements:
                if item.start in seen_starts:
                    continue
                seen_starts.add(item.start)
                measurements.append(item)

        measurements.sort(key=lambda item: item.start)
        if measurements:
            LOGGER.debug(
                "Interval data: %d of %d day(s) fetched with granularity=%s "
                "(%d already imported) -> %d measurements",
                len(wanted),
                window,
                self._interval_granularity,
                skipped_days,
                len(measurements),
            )
        elif skipped_days:
            # Part of the window was skipped, so an empty result says nothing about
            # whether this meter has sub-daily data. Do not treat it as a verdict.
            LOGGER.debug(
                "Interval data: the %d day(s) not yet imported returned nothing; "
                "Fluvius has probably not published them yet",
                len(wanted),
            )
        elif self._interval_granularity is None:
            # Every candidate was tried on every day of the window and none returned
            # anything: this meter has no sub-daily data. Stop asking — the probe
            # costs one request per candidate per day, every refresh.
            self._interval_unavailable = True
            LOGGER.warning(
                "No %d-minute data for this %s meter after probing %d day(s) back to %s. "
                "Outcome per granularity code: %s. Fluvius is not serving sub-daily data "
                "for meter %s, so the interval fetch is now disabled for this entry; "
                "daily sensors are unaffected. Reload the entry to probe again.",
                self._expected_interval_minutes(),
                self._meter_type,
                window,
                self._build_interval_range(window).get("historyFrom", "")[:10],
                self._format_probe_report(),
                self._meter_serial,
            )
        else:
            # The code is known to work, so this is a transient gap (publication
            # delay, outage). Keep trying on the next refresh.
            LOGGER.warning(
                "No interval data this refresh for meter %s, although granularity=%s is "
                "known to work. Fluvius may not have published the requested days yet.",
                self._meter_serial,
                self._interval_granularity,
            )
        return measurements

    def _day_already_held(
        self,
        day_offset: int,
        skip_hours: Optional[Set[datetime]],
    ) -> bool:
        """Whether every hour of this day is already accounted for elsewhere.

        A partially covered day is re-fetched: Fluvius publishes days in one go, so a
        gap means the day was incomplete when it was first read.
        """

        if not skip_hours:
            return False
        return all(hour in skip_hours for hour in self._interval_day_hours(day_offset))

    def _format_probe_report(self) -> str:
        """Render the per-candidate probe outcomes for a single log line."""

        if not self._probe_outcomes:
            return "no request was made"
        return ", ".join(
            f"granularity={code}: {outcome}"
            for code, outcome in sorted(self._probe_outcomes.items())
        )

    async def _async_fetch_interval_day(
        self,
        access_token: str,
        day_offset: int,
    ) -> List[FluviusIntervalMeasurement]:
        """Fetch one day, resolving the granularity code on first use."""

        if self._interval_granularity is not None:
            payload = await self._fetch_raw_interval(
                access_token, day_offset, self._interval_granularity
            )
            measurements = self._interval_from_payload(payload)
            LOGGER.debug(
                "Interval data: day -%d gave %d raw intervals -> %d parsed measurements",
                day_offset,
                len(payload),
                len(measurements),
            )
            return measurements

        return await self._async_probe_granularity(access_token, day_offset)

    async def _async_probe_granularity(
        self,
        access_token: str,
        day_offset: int,
    ) -> List[FluviusIntervalMeasurement]:
        """Try each candidate code until one returns the expected interval length.

        Nothing is remembered until real data comes back: an empty payload only means
        Fluvius has not published that day yet, so it must not disqualify a code.
        """

        expected = self._expected_interval_minutes()
        best_effort: Optional[tuple[str, List[FluviusIntervalMeasurement], int]] = None

        for candidate in self._granularity_candidates():
            try:
                payload = await self._fetch_raw_interval(access_token, day_offset, candidate)
            except FluviusApiError as err:
                LOGGER.debug("Interval probe: granularity=%s rejected (%s)", candidate, err)
                self._probe_outcomes[candidate] = f"request failed ({err})"
                continue

            measurements = self._interval_from_payload(payload)
            detected = self._detect_interval_minutes(measurements)
            self._probe_outcomes[candidate] = (
                "no data" if detected is None else f"{detected}-minute intervals"
            )

            if detected == expected:
                LOGGER.debug(
                    "Interval probe: confirmed %d-minute intervals with granularity=%s for %s",
                    detected,
                    candidate,
                    self._meter_type,
                )
                self._interval_granularity = candidate
                return measurements

            if detected is None:
                LOGGER.debug(
                    "Interval probe: granularity=%s returned no data for day -%d",
                    candidate,
                    day_offset,
                )
                continue

            LOGGER.debug(
                "Interval probe: granularity=%s returned %d-minute intervals (expected %d)",
                candidate,
                detected,
                expected,
            )
            if best_effort is None:
                best_effort = (candidate, measurements, detected)

        if best_effort is not None:
            candidate, measurements, detected = best_effort
            LOGGER.warning(
                "No granularity code returned %d-minute intervals for this %s meter. "
                "Falling back to granularity=%s, which serves %d-minute intervals. "
                "The data is still imported at that resolution.",
                expected,
                self._meter_type,
                candidate,
                detected,
            )
            self._interval_granularity = candidate
            return measurements

        LOGGER.debug(
            "Interval probe: no candidate returned data for day -%d; will retry next refresh",
            day_offset,
        )
        return []

    def _granularity_candidates(self) -> tuple[str, ...]:
        return INTERVAL_GRANULARITY_CANDIDATES.get(
            self._meter_type, INTERVAL_GRANULARITY_CANDIDATES[METER_TYPE_ELECTRICITY]
        )

    def _expected_interval_minutes(self) -> int:
        return INTERVAL_MINUTES_BY_METER_TYPE.get(
            self._meter_type, INTERVAL_MINUTES_BY_METER_TYPE[METER_TYPE_ELECTRICITY]
        )

    def _resolve_interval_days_back(self, days_back: Optional[int]) -> int:
        """Derive the interval window from the single configured history depth.

        Same option as the daily summaries, but capped: the summaries fetch their
        whole range in one request while this endpoint needs one request per day.
        """

        raw: Any = days_back
        if raw is None:
            raw = self._options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK)
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = DEFAULT_DAYS_BACK
        if self._meter_type == METER_TYPE_GAS:
            value = max(value, GAS_MIN_INTERVAL_LOOKBACK_DAYS)
        return min(max(value, 1), MAX_INTERVAL_DAYS_BACK)

    @staticmethod
    def _detect_interval_minutes(
        measurements: List[FluviusIntervalMeasurement],
    ) -> Optional[int]:
        """Return the most frequent interval length, in minutes."""

        counts: Dict[int, int] = {}
        for item in measurements:
            minutes = int(round((item.end - item.start).total_seconds() / 60))
            if minutes <= 0:
                continue
            counts[minutes] = counts.get(minutes, 0) + 1
        if not counts:
            return None
        return max(counts, key=counts.__getitem__)

    async def _fetch_summaries_and_spikes(
        self,
        *,
        include_spikes: bool,
    ) -> tuple[List[FluviusDailySummary], List[FluviusPeakMeasurement]]:
        access_token = await self._async_get_access_token()
        payload = await self._fetch_raw_consumption(access_token)
        LOGGER.debug("Raw consumption payload has %d items", len(payload))
        if payload:
            LOGGER.debug("First payload item keys: %s", list(payload[0].keys()) if payload[0] else "empty")
        summaries = self._summaries_from_payload(payload)
        LOGGER.debug("Parsed %d summaries from payload", len(summaries))
        # Don't fail if no summaries - data may not be available yet for new setups
        # The coordinator will handle empty data gracefully

        peaks: List[FluviusPeakMeasurement] = []
        if include_spikes and self._meter_type != METER_TYPE_GAS:
            spike_payload = await self._fetch_raw_spikes(access_token)
            peaks = self._spikes_from_payload(spike_payload)
        return summaries, peaks

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    async def _async_get_access_token(self) -> str:
        self._log_verbose("Starting authentication for user: %s", self._email[:3] + "***")
        try:
            access_token, _ = await async_get_bearer_token(
                self._session,
                self._email,
                self._password,
                remember_me=self._remember_me,
                verbose=self._verbose,
            )
        except FluviusAuthError as err:
            error_msg = str(err)
            LOGGER.error(
                "FLUVIUS AUTH ERROR: Failed to authenticate with Fluvius. "
                "This usually means: 1) Invalid email/password, 2) Fluvius service is down, "
                "or 3) Your account needs re-verification at mijn.fluvius.be. "
                "Details: %s",
                error_msg,
            )
            raise FluviusApiError(
                f"Authentication failed - check your credentials or visit mijn.fluvius.be to verify your account. Error: {err}"
            ) from err
        except aiohttp.ClientError as err:
            LOGGER.error(
                "FLUVIUS NETWORK ERROR: Could not reach Fluvius authentication servers. "
                "Check your internet connection. Details: %s",
                err,
            )
            raise FluviusApiError(
                f"Network error while authenticating - check internet connection. Error: {err}"
            ) from err

        if not access_token:
            LOGGER.error(
                "FLUVIUS AUTH ERROR: Authentication completed but no access token was returned. "
                "This is unexpected - try re-authenticating or check Fluvius service status."
            )
            raise FluviusApiError(
                "Authentication succeeded but no access token was returned - try removing and re-adding the integration"
            )
        
        self._log_verbose("Authentication successful, received access token")
        return access_token

    async def _fetch_raw_consumption(self, access_token: str) -> List[Dict[str, Any]]:
        history_params = self._build_history_range()
        granularity = str(self._options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY))
        if self._meter_type == METER_TYPE_GAS:
            granularity = GAS_SUPPORTED_GRANULARITY
        params = {
            **history_params,
            "granularity": granularity,
            "asServiceProvider": "false",
            "meterSerialNumber": self._meter_serial,
        }
        
        self._log_verbose(
            "API Request - URL: meter-measurement-history/%s, Params: granularity=%s, from=%s, until=%s, meter=%s",
            self._ean,
            granularity,
            history_params.get("historyFrom", ""),
            history_params.get("historyUntil", ""),
            self._meter_serial,
        )
        
        LOGGER.debug(
            "Fetching consumption: granularity=%s, from=%s, until=%s",
            granularity,
            history_params.get("historyFrom", "")[:10],
            history_params.get("historyUntil", "")[:10],
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (HomeAssistant-FluviusEnergy)",
        }
        url = f"https://mijn.fluvius.be/verbruik/api/meter-measurement-history/{self._ean}"

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=30) as response:
                self._log_verbose(
                    "API Response - Status: %s, Content-Type: %s",
                    response.status,
                    getattr(response, 'content_type', 'unknown'),
                )
                if response.status != 200:
                    response_text = await response.text()
                    self._log_verbose("API Error Response Body: %s", response_text[:500])
                    LOGGER.error(
                        "FLUVIUS API ERROR: Consumption API returned HTTP %s. "
                        "EAN: %s, Meter: %s. Response: %s",
                        response.status,
                        self._ean,
                        self._meter_serial,
                        response_text[:200],
                    )
                response.raise_for_status()
                data: Any = await response.json()
        except aiohttp.ClientResponseError as err:
            LOGGER.error(
                "FLUVIUS API ERROR: Failed to fetch consumption data. HTTP Status: %s, Reason: %s. "
                "This could mean: 1) EAN '%s' is invalid, 2) Meter serial '%s' doesn't match, "
                "or 3) Fluvius API is experiencing issues.",
                err.status,
                err.message,
                self._ean,
                self._meter_serial,
            )
            raise FluviusApiError(
                f"Consumption API call failed (HTTP {err.status}): {err.message}. Check EAN and meter serial."
            ) from err
        except aiohttp.ClientError as err:
            LOGGER.error(
                "FLUVIUS NETWORK ERROR: Could not fetch consumption data. "
                "Check internet connection. Error: %s",
                err,
            )
            raise FluviusApiError(f"Consumption API call failed - network error: {err}") from err
        except ValueError as err:  # pragma: no cover - defensive
            LOGGER.error("FLUVIUS API ERROR: Received invalid JSON from Fluvius: %s", err)
            raise FluviusApiError(f"Failed to decode Fluvius JSON response: {err}") from err

        if not isinstance(data, list):
            LOGGER.error(
                "FLUVIUS API ERROR: Unexpected response format. Expected list, got %s. "
                "Response preview: %s",
                type(data).__name__,
                str(data)[:200],
            )
            raise FluviusApiError(
                f"Fluvius API returned unexpected response type: {type(data).__name__} (expected list)"
            )
        
        self._log_verbose("API Response - Received %d items in payload", len(data))
        if data and self._verbose:
            self._log_verbose("First item keys: %s", list(data[0].keys()) if data[0] else "empty")
        
        return data

    def _build_history_range(self) -> Dict[str, str]:
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        days_back = max(int(self._options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK)), 1)

        if self._meter_type == METER_TYPE_GAS:
            days_back = max(days_back, GAS_MIN_LOOKBACK_DAYS)

        local_now = datetime.now(tzinfo)
        # Daily granularity accepts multi-day ranges ending today.
        start_date = (local_now - timedelta(days=days_back)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_date = local_now.replace(hour=23, minute=59, second=59, microsecond=999000)

        return {
            "historyFrom": start_date.isoformat(timespec="milliseconds"),
            "historyUntil": end_date.isoformat(timespec="milliseconds"),
        }

    async def _fetch_raw_interval(
        self,
        access_token: str,
        day_offset: int,
        granularity: str,
    ) -> List[Dict[str, Any]]:
        """Fetch one day of raw sub-daily consumption data from the API."""
        history_params = self._build_interval_range(day_offset)
        params = {
            **history_params,
            "granularity": granularity,
            "asServiceProvider": "false",
            "meterSerialNumber": self._meter_serial,
            **INTERVAL_EXTRA_PARAMS,
        }

        self._log_verbose(
            "Interval API Request - from=%s, until=%s, day_offset=%d",
            history_params.get("historyFrom", ""),
            history_params.get("historyUntil", ""),
            day_offset,
        )

        LOGGER.debug(
            "Fetching interval data: granularity=%s, from=%s, until=%s, meter=%s",
            granularity,
            history_params.get("historyFrom", ""),
            history_params.get("historyUntil", ""),
            self._meter_serial,
        )

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (HomeAssistant-FluviusEnergy)",
        }
        url = f"https://mijn.fluvius.be/verbruik/api/meter-measurement-history/{self._ean}"

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=30) as response:
                self._log_verbose("Interval API Response - Status: %s", response.status)
                if response.status != 200:
                    response_text = await response.text()
                    LOGGER.warning(
                        "FLUVIUS API WARNING: Interval API returned HTTP %s. "
                        "This data may not be available for your meter. Response: %s",
                        response.status,
                        response_text[:200],
                    )
                response.raise_for_status()
                data: Any = await response.json()
        except aiohttp.ClientResponseError as err:
            LOGGER.warning(
                "FLUVIUS API WARNING: Could not fetch interval data (HTTP %s). "
                "15-minute interval data may not be available for meter %s.",
                err.status,
                self._meter_serial,
            )
            raise FluviusApiError(
                f"Interval consumption API failed (HTTP {err.status}) - this data may not be available for your meter"
            ) from err
        except aiohttp.ClientError as err:
            LOGGER.warning("FLUVIUS NETWORK WARNING: Could not fetch interval data: %s", err)
            raise FluviusApiError(f"Interval consumption API call failed: {err}") from err
        except ValueError as err:  # pragma: no cover - defensive
            raise FluviusApiError(f"Failed to decode Fluvius JSON: {err}") from err

        if not isinstance(data, list):
            raise FluviusApiError("Fluvius API returned an unexpected payload (expected list)")
        
        self._log_verbose("Interval API Response - Received %d intervals", len(data))
        if data:
            self._log_verbose("Interval raw first interval: %s", data[0])
            self._log_verbose("Interval raw last interval: %s", data[-1])
        return data

    def _interval_day_bounds(self, day_offset: int) -> tuple[datetime, datetime]:
        """Return the local start and end of a single past day.

        Interval data requires SINGLE DAY requests and is only published for days
        that are already closed, so `day_offset` is counted back from today: 1 means
        yesterday, 2 the day before, and so on. Today is never available.

        The window must line up with the meter's own day, or Fluvius answers HTTP 200
        with an empty list rather than a partial result. Electricity uses calendar
        days (00:00 -> 00:00); gas uses the gas day, 06:00 -> 06:00 the next morning.
        """
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        local_now = datetime.now(tzinfo)
        start_hour = GAS_DAY_START_HOUR if self._meter_type == METER_TYPE_GAS else 0

        start = (local_now - timedelta(days=max(day_offset, 1))).replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
        # Wall-clock +1 day, so the span stays a real day across DST changes.
        return start, start + timedelta(days=1)

    def _build_interval_range(self, day_offset: int) -> Dict[str, str]:
        start, end = self._interval_day_bounds(day_offset)
        # One millisecond short of the next day, so the window never bleeds into it.
        return {
            "historyFrom": start.isoformat(timespec="milliseconds"),
            "historyUntil": (end - timedelta(milliseconds=1)).isoformat(
                timespec="milliseconds"
            ),
        }

    def _interval_day_hours(self, day_offset: int) -> List[datetime]:
        """UTC hours this day spans, matching how statistics are bucketed.

        Walks in UTC rather than local time so a DST day yields its real 23 or 25
        hours instead of a fixed 24.
        """
        start, end = self._interval_day_bounds(day_offset)
        cursor = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        end_utc = end.astimezone(timezone.utc)

        hours: List[datetime] = []
        while cursor < end_utc:
            hours.append(cursor)
            cursor += timedelta(hours=1)
        return hours

    def interval_window_start(self, days_back: Optional[int] = None) -> datetime:
        """UTC start of the oldest day the interval fetch would cover."""

        window = self._resolve_interval_days_back(days_back)
        start, _ = self._interval_day_bounds(window)
        return start.astimezone(timezone.utc)

    async def _fetch_raw_spikes(self, access_token: str) -> List[Dict[str, Any]]:
        spike_params = self._build_spike_history_range()
        params = {
            **spike_params,
            "asServiceProvider": "false",
            "meterSerialNumber": self._meter_serial,
        }
        
        self._log_verbose(
            "Peak power API Request - from=%s, until=%s",
            spike_params.get("historyFrom", "")[:10],
            spike_params.get("historyUntil", "")[:10],
        )
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (HomeAssistant-FluviusEnergy)",
        }
        url = f"https://mijn.fluvius.be/verbruik/api/meter-measurement-spikes/{self._ean}"

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=30) as response:
                self._log_verbose("Peak power API Response - Status: %s", response.status)
                if response.status != 200:
                    response_text = await response.text()
                    LOGGER.warning(
                        "FLUVIUS API WARNING: Peak power API returned HTTP %s. Response: %s",
                        response.status,
                        response_text[:200],
                    )
                response.raise_for_status()
                data: Any = await response.json()
        except aiohttp.ClientResponseError as err:
            LOGGER.warning(
                "FLUVIUS API WARNING: Could not fetch peak power data (HTTP %s). "
                "Peak power data may not be available for meter %s.",
                err.status,
                self._meter_serial,
            )
            raise FluviusApiError(f"Peak power API call failed (HTTP {err.status})") from err
        except aiohttp.ClientError as err:
            LOGGER.warning("FLUVIUS NETWORK WARNING: Could not fetch peak power data: %s", err)
            raise FluviusApiError(f"Peak power API call failed: {err}") from err
        except ValueError as err:  # pragma: no cover - defensive
            raise FluviusApiError(f"Failed to decode Fluvius JSON: {err}") from err

        if not isinstance(data, list):
            raise FluviusApiError("Fluvius spike API returned an unexpected payload (expected list)")
        
        self._log_verbose("Peak power API Response - Received %d items", len(data))
        return data

    def _build_spike_history_range(self) -> Dict[str, str]:
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        local_now = datetime.now(tzinfo)
        start_date = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end_date = local_now.replace(hour=23, minute=59, second=59, microsecond=999000)
        return {
            "historyFrom": start_date.isoformat(timespec="milliseconds"),
            "historyUntil": end_date.isoformat(timespec="milliseconds"),
        }

    def _resolve_timezone(self, tz_name: Optional[str]):
        if tz_name and ZoneInfo is not None:
            try:
                return ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:  # pragma: no cover - fallback path
                pass
        if tz_name:
            # The provided timezone string exists but zoneinfo is unavailable.
            pass
        local = datetime.now().astimezone().tzinfo
        if local:
            return local
        return timezone.utc

    # ------------------------------------------------------------------
    # Payload parsing helpers
    # ------------------------------------------------------------------
    def _target_unit_code(self) -> int | None:
        """Return the unit code that should be processed for this entry."""

        if self._meter_type != METER_TYPE_GAS:
            return None
        gas_unit = str(self._options.get(CONF_GAS_UNIT, DEFAULT_GAS_UNIT))
        if gas_unit == GAS_UNIT_CUBIC_METERS:
            return CUBIC_METER_UNIT_CODE
        return KILO_WATT_HOUR_UNIT_CODE

    def _summaries_from_payload(self, payload: List[Dict[str, Any]]) -> List[FluviusDailySummary]:
        summaries: List[FluviusDailySummary] = []
        for i, day_data in enumerate(payload):
            summary = self._summarize_day(day_data)
            if summary:
                summaries.append(summary)
            else:
                LOGGER.debug("Could not parse day_data at index %d: d=%s", i, day_data.get("d"))
        summaries.sort(key=lambda item: item.start)
        return summaries

    def _spikes_from_payload(self, payload: List[Dict[str, Any]]) -> List[FluviusPeakMeasurement]:
        peaks: List[FluviusPeakMeasurement] = []
        for chunk in payload:
            period_start = self._parse_datetime(chunk.get("d"))
            period_end = self._parse_datetime(chunk.get("de")) or period_start
            if not period_start or not period_end:
                continue
            for reading in chunk.get("v", []) or []:
                value = self._safe_float(reading.get("v"))
                spike_start = self._parse_datetime(reading.get("sst"))
                spike_end = self._parse_datetime(reading.get("set"))
                if spike_start is None or spike_end is None:
                    continue
                peaks.append(
                    FluviusPeakMeasurement(
                        period_start=period_start,
                        period_end=period_end,
                        spike_start=spike_start,
                        spike_end=spike_end,
                        value_kw=value,
                    )
                )
        peaks.sort(key=lambda item: item.period_start)
        return peaks

    def _interval_from_payload(
        self,
        payload: List[Dict[str, Any]],
    ) -> List[FluviusIntervalMeasurement]:
        """Parse the raw API response into interval measurements.
        
        Each item in the payload represents one interval with:
        - d: start datetime (ISO format)
        - de: end datetime (ISO format)
        - v: array of values with t=1 for consumption, t=2 for injection
        """
        measurements: List[FluviusIntervalMeasurement] = []
        target_unit = self._target_unit_code()
        skipped_intervals = 0
        skipped_units: Dict[int, int] = {}
        unknown_types: Dict[int, int] = {}

        for interval in payload:
            start = self._parse_datetime(interval.get("d"))
            end = self._parse_datetime(interval.get("de"))
            if not start or not end:
                skipped_intervals += 1
                continue

            consumption = 0.0
            injection = 0.0

            for reading in interval.get("v", []) or []:
                value_type = self._safe_int(reading.get("t"))  # 1=consumption, 2=injection
                unit = self._safe_int(reading.get("u"))
                value = self._safe_float(reading.get("v"))

                # Skip readings that don't match the target unit (for gas meters)
                if target_unit is not None and unit != target_unit:
                    skipped_units[unit] = skipped_units.get(unit, 0) + 1
                    continue
                # Skip volume readings for electricity meters
                if target_unit is None and unit == CUBIC_METER_UNIT_CODE:
                    skipped_units[unit] = skipped_units.get(unit, 0) + 1
                    continue

                if value_type == 1:
                    consumption += value
                elif value_type == 2:
                    injection += value
                else:
                    unknown_types[value_type] = unknown_types.get(value_type, 0) + 1

            measurements.append(
                FluviusIntervalMeasurement(
                    start=start,
                    end=end,
                    consumption=consumption,
                    injection=injection,
                )
            )

        if skipped_intervals:
            LOGGER.debug(
                "Interval data: dropped %d interval(s) with an unparsable 'd'/'de' timestamp",
                skipped_intervals,
            )
        if skipped_units:
            LOGGER.debug("Interval data: ignored readings per unit code: %s", skipped_units)
        if unknown_types:
            LOGGER.debug(
                "Interval data: readings with an unhandled type 't' (expected 1 or 2): %s",
                unknown_types,
            )

        measurements.sort(key=lambda item: item.start)
        return measurements

    def _summarize_day(self, day_data: Dict[str, Any]) -> Optional[FluviusDailySummary]:
        start = self._parse_datetime(day_data.get("d"))
        if not start:
            return None
        end = self._parse_datetime(day_data.get("de")) or (start + timedelta(days=1))
        metrics: Dict[str, float] = {metric: 0.0 for metric in ALL_METRICS}
        target_unit = self._target_unit_code()

        for reading in day_data.get("v", []) or []:
            direction = self._safe_int(reading.get("dc"))
            tariff = self._safe_int(reading.get("t"), default=1)
            unit = self._safe_int(reading.get("u"))
            value = self._safe_float(reading.get("v"))

            if target_unit is not None and unit != target_unit:
                # Skip duplicate gas readings in the non-selected unit.
                continue
            if target_unit is None and unit == CUBIC_METER_UNIT_CODE:
                # Gas meters return both m3 and kWh. Skip the volume reading when
                # keeping the default energy-based sensors.
                continue

            metric_key = self._metric_from_reading(direction, tariff)
            if not metric_key:
                continue
            metrics[metric_key] += value

        metrics["consumption_total"] = metrics["consumption_high"] + metrics["consumption_low"]
        metrics["injection_total"] = metrics["injection_high"] + metrics["injection_low"]
        metrics["net_consumption"] = metrics["consumption_total"] - metrics["injection_total"]

        day_id = start.isoformat()
        return FluviusDailySummary(day_id=day_id, start=start, end=end, metrics=metrics)

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        fixed = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(fixed)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _metric_from_reading(direction: int, tariff: int) -> Optional[str]:
        """Return the metric bucket that should be incremented for a reading."""

        is_high_tariff = tariff == 1
        if direction == 0:
            return "consumption_high" if is_high_tariff else "consumption_low"
        if direction == 1:
            return "consumption_high" if is_high_tariff else "injection_high"
        if direction == 2:
            return "consumption_low" if is_high_tariff else "injection_low"
        return None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return default
