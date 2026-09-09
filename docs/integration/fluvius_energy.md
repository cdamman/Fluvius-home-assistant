# Fluvius Energy Home Assistant Integration

The Fluvius Energy custom integration authenticates against the Mijn Fluvius portal using the Azure B2C PKCE flow and exposes long-term electricity consumption/injection sensors alongside gas consumption sensors that can report kWh or cubic meters. When adding the integration you can now choose whether the entry targets an electricity or gas meter so polling cadence and parsing logic line up with Fluvius' delivery schedule.

## Configuration

1. Copy `custom_components/fluvius` into your Home Assistant `config/custom_components` directory.
2. Restart Home Assistant to load the new integration.
3. Navigate to **Settings -> Devices & Services -> Add Integration** and search for **Fluvius Energy**.
4. Provide the same credentials you use on mijn.fluvius.be:
   - Email address
   - Password
   - EAN number
   - Meter serial number
   - Meter type (electricity or gas)
5. After the first data refresh, open the Energy dashboard configuration and map:
   - `sensor.fluvius_consumption_total` -> Grid consumption
   - `sensor.fluvius_injection_total` -> Return to grid (production)
   - Optional tariff-specific sensors for advanced dashboards

## Options

The integration exposes an options flow so you can fine-tune historic lookback and date handling without re-adding the entry:

- **Timezone** - IANA timezone used when requesting history (Europe/Brussels by default)
- **Days back** - How much history to keep (1-31). One option drives both endpoints: the daily summaries fetch their whole range in a single request, while the detailed interval data needs one request per day and is capped at 14. Gas entries enforce a 7-day minimum, since Fluvius releases gas with a ~72-hour delay. See *Fetching only what is missing* below for what a refresh actually costs.
- **Meter type** - Switch between electricity and gas if you migrate the entry later. This updates the config entry in addition to storing the other options.
- **Gas unit** - Choose whether gas entries report kWh (default) or cubic meters. Changing this option clears cached statistics to avoid mixing units.

## Diagnostics

Use **Settings -> Devices & Services -> Fluvius Energy -> Diagnostics** to download a sanitized JSON payload containing:

- Configured EAN and meter serial
- Cached lifetime totals
- Latest day summary
- Quarter-hourly coverage: interval count, covered range, totals, and the first/last few intervals
- Store bookkeeping information

This helps triage support issues without revealing passwords or bearer tokens.

## Reauthentication

If your Fluvius credentials change or expire, Home Assistant will prompt you to reauthenticate. The reauth flow validates the new credentials before updating the stored config entry and reloading the integration.

## Sensors

Electricity meters expose `state_class=total_increasing` energy sensors in kWh that can be added to the Energy dashboard:

- Total consumption
- Consumption (high tariff)
- Consumption (low tariff)
- Total injection
- Injection (high tariff)
- Injection (low tariff)
- Net consumption for the latest day
- Monthly peak power in kW (capacity tariff)

### Gas meters

Fluvius returns two readings per gas interval: volume in m3 and energy in kWh. The integration keeps the kWh readings by default so `sensor.fluvius_consumption_total` can plug directly into the Energy dashboard. If you switch the Gas unit option to cubic meters, the sensors change to `device_class=gas` with m3 units and ignore the kWh duplicate. Injection metrics remain zero for gas meters because Fluvius does not expose gas injection.

Gas measurements are released more slowly than electricity (typically 72 hours). To avoid missing late-arriving datapoints the integration always requests at least seven days of history for gas entries regardless of the configured lookback in the options flow.

## Removal

When you no longer need a Fluvius entry or want to uninstall the integration entirely:

1. Go to **Settings -> Devices & Services** in Home Assistant.
2. Select the Fluvius Energy config entry you want to remove and choose **Delete**. This unloads the platforms and deletes cached credentials/statistics for that entry.
3. If you want to fully remove the custom component, delete `custom_components/fluvius` from your Home Assistant configuration directory and restart Home Assistant.

## Testing

Basic config-flow tests live under `tests/components/fluvius/` and exercise:

- Successful user setup
- Invalid credential handling
- Reauthentication flow

Add additional tests as you expand the integration (coordinators, sensors, diagnostics, etc.).

## Detailed interval data and the Energy dashboard

Flemish digital meters register **electricity per quarter-hour** and **gas per
hour**, and Fluvius publishes both one day late (gas closer to three days late). A
Home Assistant sensor state always describes *now*, so it cannot carry that
timeline: feeding it to the Energy dashboard would attribute yesterday's
consumption to the moment of the poll.

The integration therefore writes the interval data as **external long-term
statistics**, aggregated into the hourly buckets Home Assistant requires:

| Meter | Statistic ids |
| --- | --- |
| Electricity | `fluvius:<ean>_quarter_hourly_consumption`, `fluvius:<ean>_quarter_hourly_injection` |
| Gas | `fluvius:<ean>_hourly_consumption` |

Gas gets no injection series, since a gas meter only ever consumes.

Pick these in **Settings -> Dashboards -> Energy -> Configure**. They are inserted
with their real timestamps, are idempotent across refreshes (already-imported hours
are skipped), and keep a running sum so the dashboard sees a monotonic counter.

Widening **Interval days back** after the fact does backfill: when hours older
than the newest stored one arrive, the running sum is recomputed over the whole
series so it stays consistent, and the rewritten rows replace the old ones rather
than duplicating them. The rebuild is logged at INFO and only runs when something is
genuinely missing -- the steady-state refresh just appends the newest hours.

### Fetching only what is missing

The interval endpoint accepts one day per request, so a wide window could mean a
dozen requests every hour for data that was already imported. Before fetching, the
integration reads back which hours the consumption statistic already holds and skips
any day that is fully covered. In steady state that leaves a single day -- the one
Fluvius has just published -- and when even that is already in, no request is made
at all, not even the authentication round-trip.

A partially covered day is re-fetched rather than assumed complete: Fluvius
publishes a day in one go, so a gap means the day was still incomplete when it was
first read.

The measurements are also cached in memory between refreshes. They are historical
and immutable once published, so the sensors keep showing the last published day
even on refreshes that fetch nothing. The cache starts empty after a restart, which
is why the first refresh of a Home Assistant run reads the whole window back.

### Granularity codes and day boundaries

Fluvius does not document the granularity codes its API accepts. Three are now
established against the live API:

| Code | Meaning |
| --- | --- |
| `1` | 15-minute intervals (electricity) |
| `2` | hourly intervals (gas) |
| `4` | daily values |

The interval endpoint also requires the requested window to line up with the
meter's own day, otherwise it answers HTTP 200 with an **empty list** rather than a
partial result:

| Meter | Day window (local time) |
| --- | --- |
| Electricity | `00:00` -> `23:59:59.999` |
| Gas | `06:00` -> `05:59:59.999` next morning |

Both carry `mandateDisplayMode=1`, as the portal's own requests do.

The 06:00 boundary is the Belgian *gas day*. Requesting a gas meter on calendar-day
boundaries returns nothing at all, which is what made gas look unsupported.

The client still probes the candidate codes and measures the interval length
actually returned, so a wrong guess self-corrects and is logged:

```
Interval probe: confirmed 60-minute intervals with granularity=2 for gas
```

If every candidate comes back empty for every day of the window, the client
concludes the meter has no sub-daily data, says so once with the full per-code
report, and stops asking until the entry is reloaded -- the probe costs one request
per candidate per day, so leaving it running would burn requests every hour.

### Sensors

The matching `sensor.*_quarter_hourly_*` (electricity) and `sensor.*_hourly_*`
(gas) entities deliberately declare no state class. Their state is the total of the
last published day, with the per-interval breakdown in an attribute named after the
entity key, and the statistic id in `statistic_id`.
