"""Constants for the Fluvius Energy integration."""
from __future__ import annotations

from datetime import timedelta
from homeassistant.const import Platform

DOMAIN = "fluvius"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_EAN = "ean"
CONF_METER_SERIAL = "meter_serial"
CONF_DAYS_BACK = "days_back"
CONF_GRANULARITY = "granularity"
CONF_TIMEZONE = "timezone"
CONF_REMEMBER_ME = "remember_me"
CONF_METER_TYPE = "meter_type"
CONF_GAS_UNIT = "gas_unit"
CONF_VERBOSE_LOGGING = "verbose_logging"

DEFAULT_TIMEZONE = "Europe/Brussels"
DEFAULT_DAYS_BACK = 7
DEFAULT_GRANULARITY = "4"
DEFAULT_REMEMBER_ME = False
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=60)
DEFAULT_METER_TYPE = "electricity"
DEFAULT_GAS_UNIT = "kwh"
DEFAULT_VERBOSE_LOGGING = False
GAS_UNIT_KWH = "kwh"
GAS_UNIT_CUBIC_METERS = "m3"
GAS_UNIT_OPTIONS = (GAS_UNIT_KWH, GAS_UNIT_CUBIC_METERS)
METER_TYPE_ELECTRICITY = "electricity"
METER_TYPE_GAS = "gas"
METER_TYPE_OPTIONS = (METER_TYPE_ELECTRICITY, METER_TYPE_GAS)
GAS_MIN_LOOKBACK_DAYS = 7
GAS_SUPPORTED_GRANULARITY = "4"

# The daily summary endpoint is the only consumer of CONF_GRANULARITY, and it only
# behaves with the daily code: sub-daily codes reject the multi-day ranges the
# summaries need and answer with an empty payload. Sub-daily data has its own request
# path (see INTERVAL_GRANULARITY_CANDIDATES), so nothing is lost by forcing this.
SUMMARY_GRANULARITY = "4"

# Sub-daily ("interval") consumption data.
#
# Flemish digital meters register electricity per quarter-hour and gas per hour, so
# the resolution to expect depends on the meter type. Fluvius does not document the
# granularity codes: "1" is confirmed to yield 15-minute intervals, "4" daily ones,
# and the rest is guesswork. Rather than hard-code a guess, the client probes the
# candidates below and keeps the first one that actually returns the expected
# interval length, logging what it settled on.
INTERVAL_MINUTES_BY_METER_TYPE = {
    METER_TYPE_ELECTRICITY: 15,
    METER_TYPE_GAS: 60,
}
INTERVAL_GRANULARITY_CANDIDATES = {
    # Both confirmed against the live API.
    METER_TYPE_ELECTRICITY: ("1", "3", "2"),
    METER_TYPE_GAS: ("2", "1", "3"),
}

# Fluvius bills gas on a "gas day" running 06:00 -> 06:00 local, and the interval
# endpoint returns an empty list for any window that does not line up with it.
# Electricity uses plain calendar days.
GAS_DAY_START_HOUR = 6

# Present on the interval requests the portal itself issues, for both meter types.
INTERVAL_EXTRA_PARAMS = {"mandateDisplayMode": "1"}

# The interval endpoint takes one request per day, unlike the daily summaries which
# fetch their whole range in one go. CONF_DAYS_BACK drives both, capped here so a
# deep daily history does not turn into a burst of interval requests every refresh.
MAX_INTERVAL_DAYS_BACK = 14
# Gas is published with a ~72h delay, so a short window always comes back empty.
GAS_MIN_INTERVAL_LOOKBACK_DAYS = 5

# Long term statistics fed to the Energy dashboard, keyed by EAN. The prefix keeps
# the statistic id truthful about the resolution it holds.
STATISTIC_ID_TEMPLATE = "{domain}:{ean}_{key}"
INTERVAL_KEY_PREFIX = {
    METER_TYPE_ELECTRICITY: "quarter_hourly",
    METER_TYPE_GAS: "hourly",
}
METRIC_CONSUMPTION = "consumption"
METRIC_INJECTION = "injection"


def interval_key(meter_type: str, metric: str) -> str:
    """Return the entity/statistic key for a metric at this meter's resolution."""

    prefix = INTERVAL_KEY_PREFIX.get(meter_type, INTERVAL_KEY_PREFIX[METER_TYPE_ELECTRICITY])
    return f"{prefix}_{metric}"

PLATFORMS: list[Platform] = [Platform.SENSOR]

STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = "fluvius_{entry_id}"

LIFETIME_METRICS = (
    "consumption_high",
    "consumption_low",
    "injection_high",
    "injection_low",
)

ALL_METRICS = LIFETIME_METRICS + (
    "consumption_total",
    "injection_total",
    "net_consumption",
)
