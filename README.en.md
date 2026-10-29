# China Index Futures Basis Analysis

[中文](README.md) | **English**

Computes the futures basis and option-implied basis for four Chinese equity index products
(CSI 300 / SSE 50 / CSI 500 / CSI 1000), and adjusts them with **forecast dividends** to obtain
a look-ahead-free "true" premium/discount that can be used in backtests.

No strategy backtest is included — no position simulation, no P&L.

> **Data is not included in this repository.** Market data (index / futures / ETF / options / SHIBOR)
> must be exported from a terminal such as Wind or iFinD into `data/` (see "data/ layout" below).
> Dividends, index weights, constituent prices and financials are fetched with a JoinQuant (聚宽) account.
> The dividend forecasting method replicates Debon Securities' report *Individual Stock Dividend Forecasting
> and Index Dividend Point Estimation* (2024-07-12); the report itself is not included.
> This project is for research only and is not investment advice.

---

## Layout

```
01_数据入库.ipynb    Data loading: market data from data/ + JoinQuant (dividends/weights/prices/financials) → basis.db; data checks
02_分红预测.ipynb    Dividend forecasting: model, reconciliation, backtest → dividend_forecast_daily / dividend_backtest
03_基差分析.ipynb    Basis analysis: option chains, put-call parity, basis → basis_daily; charts, HTML report, static figures
04_分红误差与基差测量.ipynb  Research question: how dividend forecast errors affect basis measurement → div_error_panel / research_*
data/                Manually exported raw data (not committed)
results/             Generated HTML reports and static figures
basis.db             SQLite database — the only channel between the three notebooks
```

Run the notebooks in order 01 → 02 → 03 → 04. The settings cell at the top of each holds all parameters and switches
(`RUN_JQ`, `RUN_BACKTEST`, `RUN_PRECOMPUTE`, …) — tune there only. The last section of 02 and 03 holds
synthetic-data checks of the conventions.

---

## Quick start

```bash
pip install -r requirements.txt
jupyter lab
```

1. `01_数据入库`: imports `data/`. To fetch from JoinQuant, put the credentials in `.env` at the project root
   (`JQ_ACCOUNT=…` / `JQ_PASS=…`; it is git-ignored) and set `RUN_JQ = True` (`JQ_FULL` for the first full fetch).
2. `02_分红预测`: on the first run, or after rules or data change, set `RUN_PRECOMPUTE = True` (about 40 minutes).
3. `03_基差分析`: computes the basis into `basis_daily`, draws charts and exports the HTML report under `results/`.
4. `04_分红误差与基差测量`: the empirical analysis on top of those tables (about 2 minutes).

### data/ layout

```
data/
├── IF_combine.csv     CSI 300     Futures side: index / futures / dividend point index / expiry / dividend points
├── IH_combine.csv     SSE 50
├── IC_combine.csv     CSI 500
├── IM_combine.csv     CSI 1000
├── 300.xlsx           Options & rates side: ETF quotes / SHIBOR / ETF option quotes
├── 50.xlsx
├── 500.xlsx
├── 1000.xlsx
├── 300期权.xlsx        Index options (IO) — separate file, reads the "期权数据" sheet
└── 1000期权.xlsx       Index options (MO)
```

Index options are accepted in either layout: a separate file `{product}期权.xlsx` with an `期权数据` sheet,
or an extra `期权数据_股指` sheet inside the main `{product}.xlsx` (as for SSE 50). Long format:
`合约代码 / 合约名称 / 日期 / 收盘价 / 成交量 / 成交额` (code / name / date / close / volume / turnover).
CSI 500 has no index options.

### JoinQuant fetching

`jq-backfill` is incremental by default: it re-fetches from 400 days before the latest ex-date in the DB,
so events stored before their implementation announcement get updated. `--full` re-fetches everything since 2010
(about 12 million rows for the four products, ~6% of a 200-million quota). If interrupted, rerun the same command.

---

## Key concept: two kinds of dividends — don't mix them up

| | Source | Good for | Not good for |
|---|---|---|---|
| **Realized dividends** `{s}_div` | Difference of the dividend point index over the window | Describing historical basis structure | **Backtesting** — it has look-ahead bias |
| **Forecast dividends** `{s}_div_pred` | Announced on the day + forecast of unannounced | **Backtesting** | — |

Realized dividends measure how much was *actually* paid between today and expiry, which the market did not know on
that day. Using them for trading-signal backtests inflates returns.

**How far apart they are** (CSI 300, next-month contract, full sample of 3,954 days, medians):

| | Basis | Annualized |
|---|---|---|
| Ex **realized** dividends | 1.24 pts | 0.30% |
| Ex **forecast** dividends | 1.78 pts | 0.46% |
| Difference | **−0.54 pts** | **−0.16 pp** |

The full-sample medians are close, but during the May–July dividend season the daily difference has a median of
1.7 points and exceeds 6.5 points on 10% of days — backtests must still use the forecast version.

In the charts, the purple dotted line is the forecast version; all other dividend lines are realized values.

---

## How dividends are forecast

Replicates the method of Debon Securities' report (2024-07-12); code in `02_分红预测.ipynb`.

**Core formula**

```
dividend points = Σ (dividend per share / share price) × constituent weight × index level
```

**Ex-date forecast** (report §2.3) — tiered by how much information is available; interval stability is judged by
the maximum absolute deviation (thresholds: plan → ex-date 20 days, shareholder meeting → ex-date 11 days):

| Situation | Rule |
|---|---|
| Implementation notice published | Ex-date is announced; use it |
| Shareholder resolution out, no implementation notice | Resolution date + stable historical interval |
| Board plan out, no resolution | Plan date + stable historical interval |
| No plan yet | Historical ex-date (latest one for the same reporting period, rolled forward by year) |
| None of the above | Default dates (7/31, 8/31, 9/30, 10/31); after 10/21 assume no dividend this year |
| 10-day rule (report §2.3(5)) | 97% of companies publish the implementation notice within 10 days before the ex-date, so without a notice the forecast date is no earlier than today + 10 days: postponed to that date if a plan exists, otherwise the default date |
| Finally | Weekend dates are moved to Friday (A-share annual ex-dates favor Fridays, twice as often as Mondays) |

Three additional rules (not in the report, but necessary in practice):

- **First-ever dividend for that reporting period, with a plan already announced** (e.g. the wave of first-time
  interim dividends since 2024): counted anyway, using the announced amount and the market-wide median interval
  (only events already ex-dividend before the forecast date, point-in-time). The old version skipped these,
  missing 120+ events per year for CSI 300 in 2024 and 2025.
- **Extrapolated date has passed but no implementation notice yet**: postponed to today + `MIN_DAYS_TO_EX`
  (10 days) per the 10-day rule, instead of being dropped.
- **Dividends going ex today are excluded**: today's close is already ex-dividend, consistent with realized
  dividends = dividend point index (expiry) − dividend point index (today). The old version included them,
  overcounting by up to 7 points on a single day in dividend season.

**Amount forecast** (report §3.2) — linear extrapolation of dividend per share, requiring dividends in each of the
last three **consecutive** years:

```
D_error = ((D₁ + D₃)/2 − D₂) / D₂  < 0.1  →  treated as linear, extrapolate
otherwise                                  →  carry forward last year's value
```

**The annual dividend is computed from the full-year total** (not in the report, necessary since 2024): extrapolate
the total dividend per share for the whole fiscal year as above, then subtract the interim / quarterly dividends
of the same fiscal year that are paid, announced or forecast. Once a company starts paying interim dividends, its
annual payment shrinks (China Merchants Bank: 2024 annual 2.00 → 2025 interim 1.01 + annual 1.00); extrapolating
the annual payment from its own history would count the interim part twice.

**Report §3.3 (net profit × payout ratio) is implemented but off by default** (enable with
`Params(profit_path=True)`): full-year net profit is projected from Q3 results per §3.3.1, and the payout ratio is
last year's (capped at 100%). It backtested slightly worse (CSI 300 90-day mean error 0.25 → 0.59; CSI 500 90-day
share within ±3 points 81% → 77%), so it is not enabled by default.

**One deliberate deviation from the report**: one forecast per reporting period (annual, interim, …), with an
inclusion threshold (at least `MIN_OCCURRENCE` occurrences in the last `LOOKBACK_YEARS` years). Effect on the
90-day window:

| Version | 90-day mean error | Share within ±3 pts |
|---|---|---|
| Only the most recent dividend | −1.52 (systematic underestimate) | 73.6% |
| Per period, no threshold | +2.31 (flips to overestimate) | 71.7% |
| Per period + threshold | −0.87 | 78.1% |
| + the three additional rules above | +0.26 | 80.8% |
| **+ 10-day rule, consecutive years, annual from full-year total** | **+0.25** | **83.2%** |

Toggling each item individually (50/300/500 × 30/60/90 days): the 10-day rule and the consecutive-year requirement
have almost no effect and are kept for consistency with the report. The full-year-total rule only changes a few
samples, mostly those starting in March 2025 / 2026; most improve (e.g. CSI 300, 90 days from 2025-03:
15.75 → 11.94 vs. actual 12.13), a few get worse (SSE 50 60-day share within ±3 points 86% → 84%).

---

## Accuracy

### Reconciliation (no forecasting — validates the formula + weights)

Section 6 of `02_分红预测`: `reconcile(conn, "300")`; `reconcile(conn, "300", roll=False)` without weight roll-forward, for comparison.

| Index | Correlation | Median abs. error | Annual cumulative error |
|---|---|---|---|
| SSE 50 | 0.9971 | 0.0007 pts | −1.0 ~ +3.9 |
| CSI 300 | 0.9946 | 0.0013 pts | −3.3 ~ +3.1 |
| CSI 500 | 0.9913 | 0.0015 pts | −3.0 ~ +2.8 |
| CSI 1000 | No dividend point index, cannot be validated | | |

The report's benchmark is ±10 points per year; here it is within ±3.3 points.

**Rolling weights forward is key to accuracy.** Month-end weights are rolled to the ex-date with price returns
(report §4.1):

```
w_nt = w_n0 × (1 + r_n) / Σ w_i0 × (1 + r_i)
```

| CSI 500 | Coverage | Median abs. error | Annual relative error |
|---|---|---|---|
| No roll-forward | — | 0.0184 pts | +1.8% ~ +16.0%, **positive in all 12 years** |
| Roll-forward | 100% | **0.0015 pts** | −3.1% ~ +2.8%, both signs |

### Forecasting

Section 5 of `02_分红预测` (`RUN_BACKTEST`); samples are written to the `dividend_backtest` table.

CSI 300, one forecast date every two months × 30/60/90-day windows, 375 samples:

| Window | Median error | Mean error | Median abs. error |
|---|---|---|---|
| 30 days | +0.11 | +0.30 | 0.58 |
| 60 days | +0.00 | +0.03 | 0.88 |
| 90 days | +0.20 | +0.25 | 1.37 |

**90.1%** of samples have `|error| ≤ 3 points` (report benchmark: rolling per-contract forecasts within ±3 points).

First version → current, per index (mean error / share within ±3 points):

| Index | 30 days | 60 days | 90 days |
|---|---|---|---|
| SSE 50 | −0.39 → +0.42 / 89% → 88% | −0.98 → −0.18 / 78% → 84% | −1.28 → +0.05 / 68% → 70% |
| CSI 300 | −0.35 → +0.30 / 89% → 98% | −0.71 → +0.03 / 77% → 90% | −0.87 → +0.25 / 69% → 83% |
| CSI 500 | −0.75 → −0.19 / 91% → 92% | −0.59 → +0.26 / 84% → 96% | −1.44 → −0.22 / 65% → 81% |

---

## Research question: how do dividend forecast errors affect basis measurement?

On the same day and contract, **basis ex forecast dividends − basis ex realized dividends = forecast − realized dividends**,
so the measurement error from adjusting the basis with forecast dividends is exactly the dividend forecast error.
`04_分红误差与基差测量` studies it on SSE 50, CSI 300 and CSI 500 across four contract slots — 37,079 observations
(2010-04 to 2026-07) — with Newey-West standard errors (lags = median remaining maturity of each slot).

| Slot | Mean annualized error (pp) | \|Error\| median, dividend season | \|Error\| median, off-season | Sign flips, season | Material flips, season |
|---|---|---|---|---|---|
| Current month | +0.46 | 0.55 | 0.04 | 6.4% | 3.3% |
| Next month | +0.10 | 0.31 | 0.08 | 4.7% | 1.3% |
| Quarter 1 | +0.01 | 0.16 | 0.09 | 2.7% | 0.1% |
| Quarter 2 | −0.07 | 0.10 | 0.16 | 1.6% | 0.0% |

Dividend season = observation date in May–August. A sign flip means the two measures disagree on premium vs. discount;
a material flip additionally requires the realized-dividend measure to be at least 1% annualized in absolute value.

- **Near-month contracts systematically overstate the premium in dividend season**: the current-month error is
  significantly positive for all three indices (CSI 300 +0.66, t = 5.2), about +2.6 pp on average in June.
  The next month is also significant for CSI 300 and CSI 500; neither quarterly slot is.
- **Quarterly contracts show no directional error**: observed in January–April their windows already cover the coming
  dividend season, so errors appear earlier, are flatter, and are diluted by the longer maturity.
- **Sign flips have increased since 2024** to about 4%–7% across maturities (mostly below 2.6% in 2015–2020),
  consistent with the surge in interim dividends.
- No trading strategy is involved, and no claim is made about arbitrage opportunities.

**Sensitivity of the two residual look-aheads** (section 8 of `02`, sections 5–6 of `04`):

- **Final implemented amounts**: on 96%–99% of dividend-season days part of the forecast relies on amounts announced but
  not yet implemented — about 70% of forecast points. No history of revisions exists; a 1% revision moves the annualized
  basis by at most 0.16 pp at the 95th percentile, 0.78 pp for a 5% revision.
- **ETF distribution announcement date**: changing "5 trading days before ex-date" to 0, 3, 10 or 20 days moves the median
  |ETF option − futures| deviation only between 12 and 16 points; the difference comes almost entirely from quarterly
  contracts, and near-month results are insensitive to the assumption.

---

## Known limitations

### Year-end / year-start windows still underestimate

Interim dividends surged after the 2024 "New Nine Articles" policy. First-time interim dividends are now counted once
a plan is announced (30 days from 2025-01-02: old forecast 0.04 points → now 5.71, actual 8.40), but first-time
dividends **without a plan yet** cannot be forecast, so 60–90-day windows starting in November still underestimate
by 2–8 points. For samples starting after July 2024, the mean error is still −0.7 to −1.3 points across the three
indices.

Tuning:

```python
sweep(Panel(conn, "300"), load_dividend_point_index(conn, "300"))                    # scan 7 parameter sets
backtest(Panel(conn, "300"), load_dividend_point_index(conn, "300"), Params(2, 1))  # a single setting
```

Selection rule: first check that the 90-day mean error is close to 0, then pick the highest share within ±3 points.
Then set `LOOKBACK_YEARS` / `MIN_OCCURRENCE` in the parameter cell of `02_分红预测` and rerun the daily precompute.

### Pending plans (plan announced, ex-date not set)

Events in JoinQuant with only a plan and no ex-date are stored in the `dividend_pending` table (the main table's
primary key includes the ex-date). It is fully refreshed per product on every `jq-backfill`. When forecasting:

- Only plans dated on or before the forecast date and no older than 365 days are used; the amount comes from the
  plan, the ex-date from plan / resolution extrapolation;
- Plans whose JoinQuant cancellation date (`bonus_cancel_pub_date`) is before the forecast date are ignored
  (applied by date, no look-ahead); progress text containing words like "stopped / terminated / rejected" is also
  ignored as a fallback; plans saying "no distribution" or with zero dividend per share stop forecasts for that
  reporting period;
- If the main table already has an event with a fixed ex-date for the same reporting period, the main table wins.

Parameters are `PENDING_*` at the top of `02_分红预测`. JoinQuant's `plan_progress` normally goes board plan →
shareholder plan → implementation (all stored events are "implementation"). The exact wording for cancellations
and "no distribution" has not been seen in real data yet; the first `jq-backfill` prints the progress distribution
of pending plans so you can check. This table is a snapshot of the current state and only affects forecasts for the
last two years; earlier backtest results are unchanged.

### Data that does not exist in the market (not a fetching gap)

| Gap | Reason |
|---|---|
| No dividend point index for CSI 1000 | Not published by CSI; CSI 1000 dividends cannot be reconciled independently |
| CSI 1000 index data only from 2022-07 | Index futures only listed in 2022 |
| No index options for CSI 500 | Not listed on CFFEX; only ETF options are available |
| No ETF options for CSI 1000 | No corresponding ETF option |
| SHIBOR only 3M, only from 2022-07 | Option-implied calculations only cover 2022 onwards anyway |

---

## Database

`status(conn)` in `01_数据入库` shows each table's coverage.

| Table | Content | Source |
|---|---|---|
| `index_daily` | Index close | Excel |
| `futures_daily` | Four futures slots, incl. expiry and realized dividend points over the window | Excel |
| `dividend_point_index` | CSI dividend point indices (300/50/500) | Excel |
| `etf_daily` / `option_quotes` | ETF quotes, ETF options per contract | Excel |
| `index_option_quotes` | Index options per contract, incl. expiry month / call-put / strike | Excel |
| `shibor` / `trading_calendar` | Interest rates, trading calendar | Excel |
| `dividend_announcement` | Dividend events with plan / shareholder meeting / implementation / record dates | JoinQuant |
| `dividend_pending` | Pending plans (no ex-date yet), fully refreshed on each fetch | JoinQuant |
| `constituent_daily` | Constituent prices (month-end + ex-date grid) | JoinQuant |
| `index_weight` | **Monthly** constituent weights | JoinQuant |
| `stock_financials` | Net profit attributable to parent, with `pub_date` | JoinQuant |
| `dividend_forecast_daily` | Daily forecast dividend points per slot | 02 |
| `forecast_exposure` | Forecast points relying on final implemented amounts, on sampled days | 02 |
| `etf_dist_detected` | Detected ETF distributions (ex-date, amount) | 03 |
| `div_error_panel` / `research_*` | Dividend forecast error panel and research results | 04 |
| `dividend_backtest` | Dividend forecast backtest samples | 02 |
| `basis_daily` | Daily basis, option-implied basis, annualized figures etc. for all four products | 03 |

Deprecated tables in old databases (`etf_distribution` / `update_log`) and redundant indexes can be dropped and the
file compacted with `vacuum(DB_PATH)` at the end of `01_数据入库` (1.55 GB → 0.95 GB).

**Point-in-time discipline**: whether information is visible on day T is judged by `plan_date` for dividends,
`impl_date` for ex-dates and `pub_date` for financials — never by `ex_date` or `report_date`.

**Known residual look-ahead**: `div_per_share` only stores the final (implemented) amount. When T falls between the
plan and implementation, the final amount — not yet visible then — is used. See the research section above for its sensitivity:
a 1% revision moves the annualized basis by at most 0.16 pp at the 95th percentile. A full fix would require storing
each stage's amount from JoinQuant.

---

## Data checks

```python
check(conn, "300")                        # 01: fields / weight frequency / share units
reconcile(conn, "300")                    # 02: dividend point reconciliation
diagnose(conn, "50", date="2018-06-07")   # 02: inspect one day
diagnose(conn, "300", worst=5)            # 02: scan the worst N days
```

`diagnose` reports "if caused by a single stock, how much its dividend per share would need to increase". If that
number is round (e.g. 1.20), most likely two dividends on the same day (regular + special) were merged and one was lost.

---

## Conventions

1. **Annualized with calendar days**: `basis / index × 365 / days to expiry`. Futures use the futures' days to
   expiry; options use their own.
2. **Ex-dividend basis = basis + dividends**. Dividends depress futures prices; adding them back removes their effect.
3. **Option-implied basis uses the same convention as futures (dividends included)**:
   - Index options are on the price index, so the forward from put-call parity already reflects dividends over the
     window: simply `forward − index`.
   - An ETF does not drop when constituents go ex-dividend (the cash stays in the fund), so the ETF forward converted
     to index points excludes dividends: add back the ETF's own distributions, then subtract window dividends.
     ETF distributions are detected from jumps in the `index / ETF` ratio.
4. **Which option columns can be backtested**: `_pred` columns only use information available on the day. The ETF
   `basis_opt` subtracts realized dividends (look-ahead); for backtests use `basis_opt_pred` / `ann_opt_pred` /
   `opt_minus_fut_pred`. `basis_opt_exdiv` (ETF) and `basis_iopt` (index options) do not depend on dividends and
   have no look-ahead.
   - **ETF option window dividends use the option's own expiry** (4th Wednesday, rolled over holidays), not the
     futures window. The few days in between can differ by up to 15 points in dividend season. Forecasts are stored
     in `dividend_forecast_daily` under slots like `IF00E`.
   - **The ETF's own distributions are added back only once announced**: amounts are detected after the fact from
     `index / ETF` jumps, 37–129 index points each, so they cannot be used early. The DB has no ETF announcement
     dates, so a distribution is treated as announced `ETF_DIST_NOTICE_DAYS` (5) trading days before its ex-date, and
     as 0 before that. The cost: when the option window spans an unannounced ETF ex-date, the ETF basis is low by
     tens of points (e.g. about −85 points for the CSI 300 far-quarter slot in June 2025) and jumps back on the
     announcement. For SSE 50 / CSI 300 backtests prefer the index-option version.
   - When realized dividends are unavailable (end of sample), `basis_opt` is left empty rather than silently
     subtracting 0.
5. **Options are matched to futures by expiry month**: take the option expiring in the same year-month as the
   futures contract, not "the n-th expiry". Index options list "current month + next 2 months + 3 quarterly months",
   futures list "current month + next month + 2 quarterly months", so matching by order picks the wrong month in the
   quarterly slots. ETF options expire on the 4th Wednesday; between the 3rd Friday and the 4th Wednesday the futures
   next-month slot has no same-month ETF option and is left empty.
6. **Index options vs. futures**: same expiry, directly comparable. Median difference (points):

   | | Current month | Next month | Quarter 1 | Quarter 2 |
   |---|---|---|---|---|
   | 50 | 0.76 | 2.45 | 4.74 | 8.55 |
   | 300 | 0.96 | 3.40 | 6.64 | 12.93 |
   | 1000 | 0.90 | 3.10 | 10.76 | 20.67 |

   A stable spread created by hedgers pushing futures down; it grows with maturity.

> 2026-10-11 fix: the old version matched options to futures by "n-th expiry", so the index-option quarterly slots
> were in the wrong month on about 68% of days (CSI 1000 quarterly "options − futures" medians were inflated to
> 44 / 88 points). Options are now matched by expiry month.

> 2026-09-16 fix: the old version subtracted window dividends twice from the index-option implied basis, so in
> dividend season `index options − futures` ≈ −dividend points (down to −25 points for the current month in
> June–July). Fixed, and `results/` has been regenerated.

---

## Convention checks

The last section of `02_分红预测` and `03_基差分析` uses synthetic data (no `basis.db` needed) to lock in the conventions: options matched to futures by expiry month,
same-day ex-dividends excluded, announced first-time dividends counted, overdue extrapolated dates postponed, the
10-day rule, consecutive-year extrapolation, net profit projection, and annual dividend net of interim.

---

## FAQ

**Plotly charts blank / won't open** — the chart library loads from a CDN; an internet connection is required.

**No purple forecast line in the charts** — the daily precompute in `02_分红预测` (`RUN_PRECOMPUTE`) hasn't been run.

**Mean / extremes of current-month annualized basis are astronomical** — near expiry only a few days remain, and
`× 365 / DTE` blows up tiny bases. **For the current month, only look at the median.**

**JoinQuant backfill is slow** — expected; tens of millions of rows for all four products. If interrupted, rerun
the same cell.

---

## License

[MIT](LICENSE)
