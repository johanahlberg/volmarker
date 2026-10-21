# VolMarker

A shared vol-marking cockpit for the FX options desk. It replaces the Excel marking sheets.

- Each pair is either **marked** (ATM, 25D RR/BF and 10D factors per tenor) or **derived** from two base pairs through the correlation triangle.
- Derived pairs update automatically whenever a base pair is published. No export button.
- A **skew layer** is applied to the Fenics file only. Front Arena always gets the pure mids.
- Every change is stored with its time and user. Concurrent edits are detected, never silently overwritten.
- Uses only the Python standard library: no server, no database, nothing to install.

## Running it

Each trader runs the app on their own PC, pointed at the same shared folder:

```
python -m volmarker --data "S:\FXO\VolMarker\data"
```

Or double-click `VolMarker.bat` after setting the data path in it. The app opens a browser tab at `http://127.0.0.1:8765`.

What one trader publishes appears in the other traders' browsers within about a second. Requires Python 3.9 or newer.

To try it with made-up demo surfaces:

```
python scripts/seed_demo.py demo_data
python -m volmarker --data demo_data
```

Tests: `python -m unittest discover -s tests -t .`

## Using it

| | |
|---|---|
| Edit a cell | Type a value. Changed cells turn yellow and nothing is sent until you **Publish** (Ctrl/Cmd+S). Up/Down/Enter move between tenors. Esc reverts the cell. |
| 10D factors | The **lock** box above each factor column (RR and BF separately, per pair). Ticked: type the factor, and 10D = 25D × factor (10D read-only). Unticked: type the 10D quote, and the factor is calibrated as 10D / 25D (factor read-only). While unticked, moving the 25D keeps the 10D quote and recalibrates the factor. |
| 3D | The surface over delta (10P…10C) × tenor, which you can rotate. Shows mid, the Fenics surface (with skew), or *draft − published* to see your unpublished changes. Updates live while you edit. |
| Parallel shifts | Toolbar: field, amount and tenor range, then **Apply to draft**. |
| Derived pairs | ATM (in blue) = triangle(base ATMs, correlation). Type an ATM to calibrate that tenor's correlation to it; the pair then keeps running off its bases. Tick **Pin** to hold a typed ATM fixed instead. |
| Skew | The Skew columns shift ATM, RR and BF in the Fenics file only. Set an expiry (or press **EOD**); when it passes, one of the running apps publishes a clean version. |
| Event weights | Weekday weights, holiday factors and additive event weights for each pair; relevant events from the Bloomberg calendar get their default weight on the right expiry automatically (see below). **Test dates**: type expiry dates (or **+ Event dates** for the day of and the day before each upcoming event) to see the interpolated ATM, weighted days and forward vol, for the draft against the published version, live while you edit weights or marks. The 6-month chart shows the draft curve against the published one. |
| History | Every version with its user and comment. **Restore** copies an old version into the draft. |
| Groups | Each pair has a **Group** (Setup tab, e.g. "Scandies", "GBP", "Metals"); the sidebar lists pairs by group. The group is a pair setting, the same for the whole desk. Pairs without one are under *Ungrouped*. |
| Your sidebar | Drag groups, or pairs within a group, into your own order. Click a group header to fold it (a folded group still shows its stale and ⚠ counts). This layout is personal and saved in `users/<your user>.json`, so it follows you to any PC. **Reset** in the sidebar footer goes back to A–Z. |
| Spread | This pair minus a reference pair (e.g. USDCHF − EURUSD) for ATM, 25D/10D RR and BF per tenor. Draft mids are compared against the reference's published mids, with the FMD spread next to it (red when further apart than the alert tolerance). Type a spread to set the pair to reference + spread in the draft, then publish as usual. The charts show the spread by tenor and its published history (1 day to 1 month). The default reference is a pair setting (**Spread reference** in Setup, or **Make … the default** in the Spread tab, then publish); without one it is a key or marked pair from the same group. The chips switch to any related pair (e.g. EURCHF against USDCHF instead of EURUSD) without changing the default. Pairs that don't need it (e.g. EURUSD) can be set to **None**, via **No spread for this pair** or Setup, and then don't show the tab. |
| Sidebar | `K` = key pair, `D` = derived, `FMD` = follows the market (green ok, amber a move is held, red not updating), `skew` = skew active, `stale` = not marked within the limit (2h for key pairs, 24h for others; set in `config.json`), `!n` = number of failed checks, `⚠n` = market alerts (grey once acknowledged). |

## Market data (Fenics Market Data)

FMD is not a set of raw quotes. It is a bootstrapped, SABR-calibrated, arbitrage-free surface built from several broker desks (BGC, GFI) and real requests on multi-dealer venues such as Fenics Direct. The **FMD composite** is therefore the default reference. The individual brands (BGC, GFI, RPM) are shown as context.

A single **feed process** holds the connection for the whole desk and writes the levels to the shared folder. Every trader's app reads them from there. There are two ways to connect, both feeding the same quote book:

| `fmd.transport` | Protocol | Credentials |
|---|---|---|
| `fix` | FIXT 1.1 / FIX 5.0 SP2 over TLS 1.2, port 8935 | `sender_comp_id` + password (`FMD_PASSWORD`) |
| `webapi` | Server-Sent Events (JSON) over HTTPS, OAuth2 client credentials; `_snapshot_age` returns the last price after a restart | `webapi.token_url`, `webapi.client_id` + secret (`FMD_CLIENT_SECRET`) |

```
python -m volmarker.fmd feed --data "S:\FXO\VolMarker\data"                      # real FMD, transport from config
python -m volmarker.fmd simulate --data demo_data [--transport webapi]             # stand-in FMD server
python -m volmarker.fmd feed --data demo_data --sim 127.0.0.1:8935                 # FIX feed from the simulator
python -m volmarker.fmd feed --data demo_data --transport webapi --sim 127.0.0.1:8936
```

The WebAPI uses the bank's HTTPS proxy from `HTTPS_PROXY`. A proxy that inspects TLS needs its CA certificate in `webapi.ca_file`.

- **Settings:** the `fmd` section of `config.json` holds the host (UAT by default), `sender_comp_id`, an optional `session_qualifier`, `product` (`fenicsMdFxo`) and extra `filters`, e.g. `[[16999, "EUR"]]`.
- **Password:** never stored in the shared folder. Set the `FMD_PASSWORD` environment variable, or point `fmd.password_file` at a file on the feed PC.
- **One feed per folder:** a second feed refuses to start while the first one is alive. The feed reconnects automatically with back-off.
- **In the grid:** broker bid/offer appears under ATM, 25D/10D RR and 25D/10D BF. A red bar on the left means the mark is outside the broker range. A red frame means an active market alert (see below).
- **Taking levels:** click a broker level to put its mid in the draft. **Market mid** does one field over a tenor range; **Market surface** takes ATM, 25D/10D RR and BF over the range. With a locked 10D factor, taking the 10D recalibrates the factor. Nothing is published automatically.
- **Brokers:** *Auto* uses the FMD aggregate first, then BGC, GFI and RPM. *Best of brands* uses the highest bid and lowest offer. A single brand can also be picked.
- **ATM:** broker ATM is matched to the pair's ATM convention (Setup tab, DNS or ATMF). A `*` means only the other convention was quoted.
- **Dailies (1D–14D):** shown in the *Event weights* tab against your weights. A market daily's forward variance divided by a normal day's is that day's weight. **Calibrate event weights to dailies** writes the implied event weights inside the 14D window into the draft, keeping weekend and holiday factors and naming events from your event list or calendar.
  - The normal-day reference is taken per segment between marked tenors (ON→1W, 1W→2W, …), because the interpolation only uses weights relative to each other within a segment. Differences in vol level between tenors therefore don't show up as fake events.
  - An event lands on the first daily expiry that contains it.
  - Dailies follow the FXO convention: "ND" = today + N calendar days, rolled to a good day for both currencies (`fmd_daily_basis`: "calendar"). Dailies that share an expiry, e.g. over a weekend, collapse into one.
- **Audit:** every manual publish stores the broker mids at that moment (`market_at_publish`) in the history.
- **Quote age:** quotes older than `market_stale_minutes` (default 30) are shown faded and skipped by **Market mid**.

## Market mode (Follow FMD)

Tick **Follow FMD** in a pair's Setup tab and publish. From then on, the FMD feed process publishes the FMD mids for ATM, 25D/10D RR and 25D/10D BF on its own. The pair goes through the normal publish, so the Fenics and Front Arena files, derived crosses, history and journal all follow. This suits pairs you don't want to mark by hand, or quiet hours.

- **You keep:** spread, skew and event weights. The market fields are read-only (pink) in the grid. Editing the spread or skew while the feed publishes is not a conflict: your publish keeps your edits and takes the feed's levels. The same applies to a derived cross whose base just moved.
- **When it publishes:** when any mark has moved at least `follow.min_change` (0.05) since the last version, and at most every `follow.min_publish_interval_s` (15 s) per pair. The history comment names the biggest move, e.g. `FMD: 1W ATM -0.06 (+3 more)`.
- **Jump guard:** a move bigger than `follow.max_jump` (ATM 1.0, 25D RR 0.75, …) is held back. It is taken only if it is still there after `follow.jump_confirm_s` (60 s). Meanwhile the pair is amber and an alert says what is held.
- **No fresh quote:** marks whose FMD quote is older than `market_stale_minutes`, or quoted only in the other ATM convention, keep their last value. The status bar above the grid counts them.
- **Feed down:** nothing is published, so Fenics keeps the last levels. The pair turns red, is flagged stale, and every app raises an alert.
- **10D:** FMD quotes the 10D directly, so on a following pair the 10D is the input and the factor is shown for information only. When you untick Follow FMD, a factor lock that can't hold the current values (factor ≤ 0) is unticked as well.
- **Taking it back:** untick Follow FMD and publish. The current levels become your marks.
- **Only marked pairs** can follow. A derived cross runs off its bases, which may themselves follow FMD.
- The follower runs inside `python -m volmarker.fmd feed`. Use `--no-follow` to run the feed without it. Its state is in `market/_follow.json`.

## Market alerts

Every app compares the **published** mids (not the draft, and without skew) of every pair with FMD, and raises an alert when they drift apart:

- **Rule:** `|our mid − FMD mid| > tolerance` (`alerts.basis: "mid"`). With `"range"`, the distance outside the FMD bid/offer is used instead.
- **Tolerances:** `alerts.tolerance` sets the default per mark (ATM 0.25, 25D RR 0.20, 10D RR 0.40, 25D BF 0.10, 10D BF 0.25). It is scaled at the short end by `alerts.tenor_scale` (ON ×2, 1W ×1.5, 2W ×1.25). Each pair can override the tolerances or mute its alerts in the Setup tab.
- **No flapping:** a breach must last `alerts.confirm_s` (20 s) before it alerts, and it clears only once the gap is below 80 % of the tolerance (`clear_ratio`).
- **Where alerts show:** the ⚠ button in the top bar (red when some are not acknowledged), the tab title, `⚠n` in the sidebar, red frames in the grid, and optional desktop notifications (tick them in the alert panel; a click opens the pair).
- **Ack:** mutes an alert (per trader) until it clears, or until the gap grows by another full tolerance.
- **Derived crosses** are checked against the FMD cross quote as well, so a stale correlation shows up as an alert.
- **Following pairs** don't get deviation alerts. They get "not updating" and "move held" alerts instead.
- **Log:** one app keeps a log of every raised and cleared alert in `alerts/<date>.jsonl`.

## Shared folder layout

```
data/
  config.json                 export folders and staleness limits
  journal.jsonl               change feed that all running apps read
  pairs/<PAIR>/history.jsonl  every version of the pair, append-only (audit trail)
  calendar/holidays.csv       ccy,date
  calendar/events.csv         ccy,date,description
  exports/fenics/<PAIR>.csv   quoted surface (ATM bid/offer, RR/BF mids)
  exports/frontarena/<PAIR>.csv  mids
  market/<PAIR>.json          broker quotes written by the FMD feed
  market/_status.json         feed connection state and heartbeat
  market/_follow.json         market-mode follower state per pair
  alerts/<date>.jsonl         alert log (raised / cleared)
  users/<user>.json           each trader's own sidebar order, folded groups, test dates
  calendar/event_defaults.csv relevant events with default weights (+ .log.jsonl)
  calendar/bloomberg/<CCY>.json  last Bloomberg ECO import per currency
  exports/weights/            Fenics message copy, FA_FX and FA_WS event files; weights_sent.jsonl
  locks/                      short-lived lock files
```

Every publish writes the vol files in the layout of the Excel marking workbook (VOL_PUBLISHER). Point the folders in `config.json` at what the macro used:

| File | `config.json` | Macro folder | Content |
|---|---|---|---|
| `<CCY1><CCY2>volsurface.txt` | `frontarena_dir` | the export folder | `vol,EUR,USD,1mo,<ATM bid>,<ATM ask>`, then `vol25`/`vol10` `strangle`/`riskreversal` mids. Pure mids, never skewed. Tenors `1d 1wk 2wk 1mo … 1yr … 5yr`. |
| `<CCY1><CCY2>volsurface.txt` | `fenics_dir` | `…\FENICSVOLS\` | `vol,atm\|s25\|rr25\|s10\|rr10,EUR,USD,1m,<bid>,<ask>`, quoted surface (skew included). RR and BF go out with bid = ask = mid, since Fenics only uses the mid. Tenors `1d 1w 2w 1m … 1y … 5y`. |
| `<CCY1><CCY2>spreads.txt` | `dc_spread_dir` | `…\DC_SPRD\` | `1m,<ATM spread × 0.5 × DC factor>`, only for pairs with a **DC spread factor** of at least 0.01 (Setup). |
| `<CCY1><CCY2>_yyyymmdd_hhmmss.csv` | `history_dir` | `…\History\` | A copy of the Fenics file, as the macro made. Not for the Follow FMD feed's publishes (every 15 s); the pair's history keeps every version anyway. |

Numbers are written like VBA (up to 15 significant digits), and lines end in CRLF. Files are written under a temporary name and then renamed, so the readers never see a half-written file.

## Event calendar and event weights

The weights follow the desk's Excel weight workbook and Fenics:

    day weight = weekday weight x holiday factor(ccy1) x holiday factor(ccy2) + events

with seven weekday weights per pair (e.g. Mon 1.05, Tue-Fri 1, Sat/Sun 0) and a holiday factor per currency (e.g. 0.4) that applies on that currency's holidays. Older records with weekend factors are read as weekday weights.

**Bloomberg import.** In the *Event weights* tab → *Event calendar*, **Import Bloomberg calendar…** takes an ECO export saved as .xlsx (one file per currency or region; eurozone country codes all count as EUR). The preview lists every distinct event with its ticker, relevance, next release and release times. Tick the relevant ones and set their default weight, then **Import with this selection**. The file replaces the previous import of that currency (`calendar/bloomberg/<CCY>.json`). The whole file is kept, so ticking another event later on the list needs no new import. Releases with only a date range are left out until Bloomberg gives a day.

**Relevant events.** Only events on the list (`calendar/event_defaults.csv`: ccy, event, weight, ticker) get a weight. An event matches on its ticker, or on its name; outside the home country the name carries the country code, as in the Excel workbook ("GE IFO Business Climate"). To start from the workbook's list: `python scripts/import_excel_weights.py "Events USD.xlsm" --data <folder>`.

**Which expiry an event weighs on.** Bloomberg times are in the terminal's time zone (`calendar_tz`, Europe/Zurich). Each pair has an **Expiry cut** in Setup (NYC 10:00 New York, TOK 15:00 Tokyo; more in `config.json` → `cuts`). An event weighs on the first good business day whose cut is after the release, so FOMC at 14:00 New York on Wednesday weighs on Thursday's expiry, while the ECB at 14:15 Frankfurt (08:15 New York) weighs on the same day. Daylight-saving gaps between Europe and the US are handled. Manual events without a time (`calendar/events.csv`, e.g. elections) weigh on their own date.

**Per pair.** The events table shows each expiry with its release time, currency, the default weight and the weight used. Several events on one expiry add up, and the biggest names the day (hover over the Σ for the breakdown). Typing a weight overrides the default for that pair (↺ goes back); 0 switches it off; **+ Event** adds a one-off. **Calibrate to dailies** writes market-implied weights the same way.

**Send weights.** **Send to Fenics & Front Arena** sends the published version, as the workbook's macro does:
- Fenics: one `gfi_message` (`load_events` / `WEIGHT_FEED`) posted as `xml=…` to `weights_export.fenics_url` + `/xmlposter`. It carries the weekday weights, the holiday base weights (FirstCcy = ccy2, SecondCcy = ccy1) and the events. An event on a holiday of its own currency is sent as a holiday weight `h + w / (weekday x other holiday)`. User and password are asked each time; the password is passed on and never stored. A copy of the message without the password is kept in `exports/weights/fenics/`.
- Front Arena: `FA_FX/<CCY1><CCY2>_events.txt` (ccy,date,event,weight per expiry) and `FA_WS/…` (moved to the business day before, weight (w + base) / base). Folders and date format in `config.json` → `weights_export` (default yyyy-mm-dd: the macro wrote the PC's regional format, which was yyyy-mm-dd on the desk PCs in Oct 2026 and dd.mm.yyyy on an earlier send).
- Every send is logged in `exports/weights_sent.jsonl`; the card says when weights, defaults or the calendar changed since the last send.

## Conventions and maths

- Vols are in percentage points. RR10 = RR25 × 10D factor, and BF10 likewise.
- Butterflies are broker strangles, both in the marks and in FMD (`S25`/`S10`), so they compare one to one. The 3D view's ATM + BF ± RR/2 smile is therefore an approximation, for display only.
- Fenics ATM bid/offer = quoted mid ± spread / 2, where quoted mid = mid + skew.
- Triangle: cross = base1^s1 × base2^s2, so σ² = σ1² + σ2² + 2·s1·s2·ρ·σ1·σ2. Here ρ is the correlation between the two base pairs **as quoted**; for example, EURUSD vs USDCHF is strongly negative. The orientation signs s1 and s2 are worked out from the currencies.
- Day weights: 1.0 on normal days. A weekend or holiday in either currency replaces it with that pair's factor (the lowest if several apply), and event weights are added on top. Total variance is interpolated linearly in weighted time between the marked tenors.
- Checks: missing ATM, non-positive bid, calendar arbitrage (total ATM variance decreasing between tenors), and a correlation outside [-1, 1] when calibrating.

## Not done yet

- Importing the existing Excel tabs and weight files
- Derived RR/BF for crosses (marked by hand for now, as in Excel)
- Inventory-driven skew (needs positions from Front Arena)
- Exact expiry dates: the date logic in `calendar.py` is approximate and only used for weighting and display
