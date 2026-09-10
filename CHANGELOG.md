# Changelog

## 1.1.0

Requires Home Assistant 2026.5 or newer. After updating, restart Home Assistant and select the new **(historical)** sources in the Energy dashboard. Old sensor history is not automatically rewritten.

- Import delayed consumption/injection at its actual measurement time, using hourly external statistics.
- Correct the electricity quarter-hour API code and fetch every day in the configured lookback window.
- Add gas hourly requests and options, gas-unit selection during setup, and valid gas sensor classes.
- Deduplicate imports across restarts and entry recreation; apply both upward and downward corrections.
- Preserve tariff breakdowns, handle daylight-saving changes, and replace daily allocations with complete interval data.
- Add an exclusive history cutoff for combining a separate Fluvius history source with a P1 meter.
- Preserve available display data on empty refreshes, isolate peak endpoint failures, cache access tokens, and trigger reauthentication when credentials fail.
- Fix option labels and verbose-logging propagation; add automated regression checks.

Validated with 41 tests on Home Assistant 2026.5, actual SQLite Recorder imports, seven days of live electricity readings, and a running Home Assistant instance. Gas daily readings were verified live; hourly gas was verified with fixtures because the test account returned no hourly data.

Professional eID/itsme login and exact 15-minute Recorder/dynamic-price accounting remain unsupported. See the README for details and upgrade instructions.
