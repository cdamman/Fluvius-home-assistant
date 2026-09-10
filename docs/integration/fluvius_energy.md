# Fluvius Energy integration

See the [installation and configuration guide](../../README.md) for setup, historical Energy sources, upgrades, gas units, and P1 cutoffs.

The normal sensor state history records update arrival times. Use the separate **(historical)** statistics for energy usage at its original measurement time.

Home Assistant imports these statistics hourly. Quarter-hour raw readings remain in the integration's storage and latest-day sensor attributes; exact 15-minute dynamic-price accounting is not implemented.
