# Final Boss — project reference and operational review

**Current operation (2026-09-26): analysis only.** The user prohibited all trades.
`ANALYSIS_ONLY` disables legacy `run`. Public market research is implemented in
`bot/scanner.py`, displayed through `bot/scanner.html`, and started with `scan`
and `scan-dashboard`. See [SCANNER.md](docs/SCANNER.md). Historical trading state
is preserved and legacy startup tasks are disabled. This reference below
describes the earlier implementation, not current authorization to trade.

Prepared on **2026-09-09** from the local source, configuration, documentation, persisted state, and runtime checks in `D:\BotTradingFinalBoss`. This document describes the implementation present in this folder; secret values are intentionally excluded. Runtime observations are time-stamped snapshots, not permanent guarantees.

## 1. Purpose and scope

Final Boss is a Python USDⓈ-M futures trading research bot for Binance Demo market data. It supports long and short positions on DOGEUSDT, SOLUSDT, ETHUSDT, and 1000PEPEUSDT. A coordinator combines deterministic specialists, checks risk, executes or simulates orders, persists accounting, and publishes reports. A sixth specialist reviews results and evaluates restricted strategy changes prospectively.

- **Paper:** local simulated fills, fees, slippage, and estimated funding using public Demo quotes; no account orders or exchange credentials required.
- **Demo:** virtual orders in a dedicated Binance Futures Demo account, using Demo-only HMAC credentials.
- **Production trading:** not implemented. The exchange client fixes its destination to `https://demo-fapi.binance.com`.
- **LLMs:** the specialists are Python modules, not six continuously running LLMs. Optional Grok classification is limited to supplied news headlines. It cannot place orders or change risk rules.
- **Profitability:** the code implements a research process; the existing results do not establish a profitable strategy.

## 2. Technology and prerequisites

| Component | Implementation |
|---|---|
| Runtime | Python 3.11+; standard library only |
| Configuration | Frozen dataclass validated against `config.toml`; `.env` and selected environment overrides |
| Market/HTTP client | `urllib`, HMAC signing, bounded retries, timestamp synchronization |
| Persistence | SQLite, WAL journal, FULL synchronous commits, JSON state and append-only events |
| Concurrency | Single trading writer protected by an OS file lock; separate daily-review worker |
| Interface | Local HTTP dashboard with HTML/CSS/JavaScript; standalone HTML/JSON exports |
| Tests | Python `unittest`; JavaScript tests using Node's built-in runner |
| Windows operation | Hidden Python process or scheduled task at user login |
| Container option | Python 3.13 slim, non-root user, persistent data/report volumes |

This checkout has no `.git` repository metadata and no dependency/package manifest. Installed executables observed during this review include `C:\Python314\python.exe`, Node.js, and Docker CLI; presence of Docker CLI does not establish that Docker Engine is running.

## 3. Repository map

| Path | Responsibility |
|---|---|
| `bot/__main__.py` | Command-line interface, run loop, logging, exports, shutdown signals |
| `bot/config.py` | Configuration defaults, TOML/environment loading, type and safety limits |
| `bot/models.py` | Candle, Snapshot, and Advice data structures |
| `bot/technical.py` | EMA, RSI, ATR, trend/momentum scoring, liquidity checks |
| `bot/context.py` | Bounded CoinDesk RSS ingestion, symbol relevance and optional Grok classification |
| `bot/risk.py` | Account state, daily/total/drawdown limits, sizing, conservative risk adaptation |
| `bot/engine.py` | Coordinator, paper fills, Demo reconciliation, protection, funding, entries/exits |
| `bot/exchange.py` | Demo-only Binance REST client and quantity/price normalization |
| `bot/storage.py` | Transactional state/events/news persistence and cross-platform process lock |
| `bot/daily.py` | Colombia-day schedule, review database, worker lifecycle, preview/review commands |
| `bot/daily_analysis.py` | Historical attribution, period metrics, specialist evaluations and proposals |
| `bot/governance.py` | Restricted parameter changes, strategy versions, application and rollback |
| `bot/shadow.py` | Forward paper comparison of baseline and candidate portfolios |
| `bot/research.py` | Small historical parameter search with chronological holdout |
| `bot/report.py` | Read-only state loading, performance metrics, atomic HTML/JSON exports |
| `bot/telemetry.py` | Agent activity, heartbeat, timing and market snapshots |
| `bot/dashboard.py`, `bot/dashboard.html` | Loopback dashboard server and user interface |
| `tests/` | Unit/integration tests and JavaScript behavior tests |
| `scripts/` | Windows start, worker, startup-task and dashboard-task helpers |
| `data/` | Trading/review databases, logs, locks, telemetry and existing backups |
| `reports/` | Generated reports, research, daily reviews and audit evidence |
| `docs/` | Grok supervision, daily-review, dashboard and visual-design references |
| `Dockerfile`, `compose.yaml` | Optional persistent container deployment |

## 4. Decision and execution flow

1. Load configuration and acquire the state-file writer lock. Restore compatible state or create a new account with the reserved virtual capital.
2. Check daily-review worker results and eligible strategy maintenance.
3. In Demo mode, reconcile orders/positions and verify protection. Request candles, bid/ask, 24-hour volume, mark prices and funding data.
4. Validate data freshness; process paper funding and existing position exits; update marked equity and account limits.
5. Analyze each new closed candle once. Persist consumed candle timestamps so restarts do not duplicate entry decisions.
6. Combine trend and momentum scores, require directional agreement, apply specialist vetoes and risk checks, then normalize order size downward to exchange rules.
7. Persist the decision and order intent, simulate a paper fill or submit a Demo order, reconcile execution, and establish protection.
8. Update prospective shadow experiments, persist state/activity, and publish HTML/JSON reports.
9. Wait the configured polling interval. Network/processing time adds to the interval; this is not a fixed 30-second deadline.

| Role | Decision contribution |
|---|---|
| Coordinator | Sequencing, final eligibility, state, reconciliation and audit trail |
| Trend | EMA20/50/200, slope and bullish/bearish/sideways regime |
| Momentum | RSI14, MACD12/26/9 and ATR14 |
| Liquidity | Spread and 24-hour quote-volume eligibility |
| Context | News availability/freshness and narrowly defined explicit incident vetoes |
| Risk/performance | Sizing, margin, aggregate exposure, loss limits and risk reduction |
| Research/validation | Daily evidence review, bounded proposals and forward comparison |

The entry score weights trend **65%** and momentum **35%**. Context is advisory/veto-only; a sentiment score does not increase the entry score or risk budget. The normal per-candle analysis contains five specialist reports; research runs separately on its schedule.

News polling now runs independently of closed-candle analysis. A failed feed
refresh immediately invalidates source freshness and vetoes new entries while
news monitoring is enabled. Each decision stores a `buy_decision` and IDs for
relevant or explicit-risk headlines. See [news decisions](docs/NEWS_DECISIONS.md)
for exact semantics and remaining source limitations.

## 5. Complete configured settings

Values below are those in `config.toml` at review time. Persisted strategy overrides and supported environment variables can change effective values; inspect the reported active strategy as well.

| Setting | Value | Meaning |
|---|---|---|
| `mode` | `paper` | Default execution mode |
| `symbols` | DOGEUSDT, SOLUSDT, ETHUSDT, 1000PEPEUSDT | Monitored futures contracts |
| `interval` | `5m` | Signal candle interval |
| `poll_seconds` | 30 | Wait after a cycle |
| `initial_equity` | 100.0 | Reserved USDT capital |
| `leverage` | 5 | Fixed supported leverage |
| `risk_per_trade` | 0.005 | Base planned loss budget, 0.5% of equity |
| `max_daily_loss_pct` | 0.05 | Daily preventive pause at 5% |
| `max_total_loss_usdt` | 30.0 | Total loss ceiling relative to starting capital |
| `max_drawdown_pct` | 0.10 | Halt at 10% below peak equity |
| `max_margin_fraction` | 0.10 | Maximum margin per position |
| `max_positions` | 2 | Simultaneous position limit |
| `entry_threshold` | 0.45 | Minimum absolute combined signal |
| `stop_atr_multiple` | 2.0 | ATR-based stop distance |
| `reward_risk_ratio` | 2.0 | Gross target distance relative to stop distance |
| `fee_rate` | 0.0005 | Paper fee estimate per side, 0.05% |
| `slippage_bps` | 5.0 | Paper adverse slippage per side |
| `max_spread_bps` | 12.0 | Entry spread ceiling |
| `min_quote_volume` | 10000000.0 | Minimum 24-hour quote volume |
| `max_funding_rate` | 0.001 | Absolute funding-rate entry limit |
| `news_enabled` | true | Read RSS news |
| `require_fresh_news` | false | An empty/old but reachable feed is not a veto by default; a failed source is |
| `grok_enabled` | false | Optional headline classifier disabled |
| `news_refresh_seconds` | 900 | News refresh/cache interval |
| `adaptation_min_trades` | 30 | Independent closed-position sample for risk adaptation |
| `max_holding_hours` | 12.0 | Time-based exit limit |
| `min_trend_score` | 0.0 | Additional trend-strength eligibility filter |
| `min_momentum_score` | 0.0 | Additional momentum-strength eligibility filter |
| `daily_review_enabled` | true | Automatic daily research |
| `daily_review_time` | `00:10` | Fixed Colombia time, UTC−5 |

Stops have a minimum distance of 0.3% of price; entries with a distance above 8% are rejected. Quantity is rounded down, never increased to meet a minimum. Orders below exchange minimum quantity/notional are skipped. `1000PEPEUSDT` uses the contract's quoted units directly.

Risk checks include unrealized PnL and reserved open-position loss. Daily risk resets use **UTC**, while research day boundaries use **Colombia UTC−5**. These are different accounting windows. The profit target is a per-trade distance, not a promised daily account return.

## 6. Credentials and external services

| Variable/service | Use |
|---|---|
| `BINANCE_DEMO_API_KEY`, `BINANCE_DEMO_API_SECRET` | Required only for authenticated Demo execution |
| `XAI_API_KEY`, `XAI_MODEL` | Required only when optional Grok classification is enabled |
| `BOT_GROK_ENABLED`, `BOT_NEWS_ENABLED` | Explicit `true`/`false` overrides of TOML flags |
| `BOT_MODE` | Compose interpolation; direct CLI uses `--mode` or TOML |
| Binance Demo REST | Market data, account/position/order endpoints and conditional stops |
| CoinDesk RSS | Dated news headlines; bounded payload and source validation |
| xAI chat completions | Optional structured classification of supplied headlines |

The loader reads `.env` relative to the current working directory and preserves environment variables already set. Do not overwrite an existing `.env` with the example. Grok inputs exclude exchange secrets; remote text/model output is treated as untrusted data, with no tools or order authority. This source folder does not itself create or connect a bot inside the Grok application.

## 7. Execution, persistence and recovery

Demo execution requires a dedicated virtual account, one-way position mode, and isolated margin. The bot sets 5x leverage. The conditional stop uses the Demo algo-order API and mark-price triggering; it remains at the exchange after the process stops. Take-profit and maximum-holding-time exits depend on the engine running. If protection cannot be confirmed, the engine attempts an exit and halts for review. Uncertain submissions are queried by client order identifier rather than blindly resent.

Paper uses bid/ask plus adverse slippage and fee estimates. Funding uses the latest observed rate. When replaying a completed candle, a stop takes precedence over a target if both are touched, and stop gaps receive the worse opening price. A long interruption can invalidate funding reconstruction and halt the sample. Entry-candle intrabar reconstruction and historical funding remain simulation limitations.

| Artifact | Purpose |
|---|---|
| `data/<mode>.sqlite3` | `state` table with account/strategy JSON; `events` table with timestamp, kind and payload |
| `data/<mode>.sqlite3-wal`, `-shm` | Active SQLite WAL companion files |
| `data/<mode>.lock` | OS writer lock; existence alone does not prove a process is running |
| `data/<mode>.log` | Rotating log, 2 MB per file and five backups |
| `data/<mode>.activity.json` | Activity spans, heartbeat and market telemetry |
| `data/<mode>.STOP` | Request to close registered positions and block entries |
| `data/<mode>.RESTART` | Run-loop restart/exit signal used by operational tooling |
| `data/<mode>.pid` | PID written by manual Windows launcher; verify identity before using |
| `data/<mode>.reviews.sqlite3`, `.reviews.lock` | Separate daily-review storage/locking |
| `reports/<mode>.json`, `.html` | Latest automatic exports |
| `reports/<mode>/reviews/` | Dated full/preview daily reports |
| `reports/research.json` | Default historical-research output |

State contains balances, peak/day references, positions, pending intents, trade records, candle cursors, halt/health fields, active strategy, daily-review status, experiments and audit history. State and its associated event are committed together. Back up a running SQLite database using its backup API or a consistent stopped snapshot; copying only the main file while WAL is active can omit recent commits.

## 8. Daily research and strategy governance

The worker reviews the previous completed Colombia day at 00:10, catching up missed completed days oldest-first. It evaluates the day, trailing 7/30 days and recorded history up to the cutoff. New trades link to entry decisions, strategy versions and specialist context; old trades without attribution remain in accounting but do not acquire invented metadata. Partial exits are grouped into verifiable closed positions for research.

Proposals require at least 30 closed positions, with additional attributable evidence for specialist filters. Each experiment changes one allowed field; capital, credentials, mode, leverage and original loss ceilings are outside optimization. Baseline and candidate receive subsequent market observations in separate paper portfolios.

Approval requires at least seven elapsed days, seven days of effective coverage and 30 closes per portfolio; candidate profitability, liquidation equity, profit factor, normalized expectancy, relative performance and drawdown must pass the programmed criteria. The documented profit-factor threshold is 1.2, or positive gains without losses. Missing/stale prices, changed baseline configuration and risk violations block approval. Failed candidates may be rejected after 14 days with sufficient data; experiments expire at 30 days.

An approved change applies only with no open positions/pending orders and a matching reference version. Version history supports rollback. Separately, daily evaluation of unprofitable independent 30-trade blocks halves exposure down to 25% of the original risk. It never increases risk to recover losses.

Historical `research` evaluates nine threshold/ATR combinations on an initial 60% training segment and evaluates the selected configuration on the final 40%. Each asset/segment has an independent 100-USDT simulation. The short downloaded window does not reproduce historical news, spreads or actual funding; results are exploratory and are not a portfolio return or promotion approval.

## 9. CLI and Windows operation

Run commands from the project root unless intentionally isolating outputs.

```powershell
cd D:\BotTradingFinalBoss
python -m unittest discover -v
node --test tests/test_dashboard_browser.js tests/test_report_browser.js
python -m bot probe
python -m bot run --mode paper --once
python -m bot run --mode paper
python -m bot status --mode paper
python -m bot health --mode paper
python -m bot report --mode paper --output reports/latest.html
python -m bot daily-review --mode paper --preview
python -m bot research --output reports/research.json
python -m bot dashboard --mode paper
```

| Command/flag | Behavior |
|---|---|
| `probe` | Public symbol metadata/market-data checks |
| `run`, `--once` | Continuous execution or exactly one cycle; can create simulated/virtual positions |
| `status` | Read-only JSON report from persisted state |
| `health` | Exit 0 for sufficiently fresh healthy state; exit 1 for unhealthy state |
| `report --output` | Write an HTML/JSON snapshot |
| `daily-review --date YYYY-MM-DD` | Review a Colombia calendar day |
| `daily-review --preview` | Preview without applying trading-state changes or experiments |
| `research --output` | Historical research artifact |
| `stop` | Write the stop request; closure needs a subsequent connected cycle |
| `resume` | Clear manual halt/stop request under lock, only with no positions/pending orders and acceptable total equity |
| `rollback` | Restore prior allowed strategy version; requires a stopped, flat instance |
| `dashboard --port` | Read-only local UI; defaults 8765 paper / 8766 Demo |
| `--config`, `--mode`, `--state` | Alternate TOML file, explicit execution mode or SQLite path |

**Isolation detail:** `--state` separates SQLite, log and telemetry, but the run loop still writes `reports/<mode>.json` and `.html` relative to its working directory. Independent audit runs should use a separate working directory plus an absolute configuration path and import path, so they do not overwrite the main instance's reports.

For continuous hidden operation, use `scripts/start-windows.ps1 -Mode paper`. `scripts/install-startup.ps1 -Mode paper` installs the `FinalBoss-paper` login task with a direct Python action and restart policy. Both support `-Preview`. `scripts/install-dashboard.ps1` manages the separate dashboard task. The PC must remain awake and connected; login startup does not run before login.

To stop deliberately: request `python -m bot stop --mode paper`, wait for empty `positions` and zero `pending_orders` in `status`, then stop the identified process/task. `Ctrl+C` or terminating the process is not a request to close positions. Resume only after diagnosing the reason for the halt; restarting alone does not clear it. Never delete the database to erase losses.

Docker Compose runs the trader with persistent `bot-data` and `bot-reports` volumes, read-only root filesystem, non-root user and a 60-second healthcheck. `BOT_MODE` defaults to paper. Unhealthy status alone does not restart the container; process exit and restart policy are separate. Do not remove volumes when history must be retained.

## 10. Dashboard and reports

The live UI binds to loopback and exposes read-only views of actual agent activity, market candles, account status, positions, decisions, reviews and experiments. It polls approximately every two seconds and retains previous data visibly when updates fail. Paper normally uses `http://127.0.0.1:8765/`; Demo uses port 8766. The service is separate from trading so interruption can still be displayed.

Standalone report HTML refreshes every 30 seconds, supports pause/manual refresh and marks cycles stale after 180 seconds. Reloading an old report does not make its data recent. Metrics include net PnL/return, realized/unrealized PnL, wins, profit factor, expectancy, drawdown, positions, halts and validation status. Recent heartbeat or passing software tests do not imply profitability.

## 11. Existing operational state found before the audit run

At **2026-09-09 11:47 Colombia / 16:47 UTC**, the main paper database was updating and two existing `pythonw` processes were visible. Detailed process command-line inspection was unavailable under the session's access restrictions; recent heartbeat/log writes demonstrate that the trading loop was active.

| Metric | Observed value |
|---|---:|
| Initial virtual equity | 100.0000 USDT |
| Equity / balance | 95.5674 USDT |
| Recorded net PnL | −4.4326 USDT |
| Recorded net return | −4.4326% |
| Reported closed trades | 25 |
| Win rate | 16.0% |
| Profit factor | 0.2480 |
| Expectancy | −0.1773 USDT per reported trade |
| Current decline from peak | 4.7623% |
| Open positions / pending orders | 0 / 0 |
| Health | Unhealthy despite a recent heartbeat |

The persisted halt reason is **`Paper tuvo interrupcion larga; financiacion incompleta, revisar muestra`**: a long paper interruption left funding reconstruction incomplete. The initial recorded metrics above therefore describe an impaired simulation sample. This review preserves the halt and the existing accounting. Baseline evidence is in `reports/audit-before-status.json`.

## 12. Runtime verification and improvement findings

The initial project reference was written before executing the isolated bot.
The 2026-09-09 observations above are historical snapshots. Subsequent work
added persistent news observations, failure detection, relevant headline IDs
and an explained buy/wait/no-buy view. These changes do not establish a
profitable strategy; consult current reports for runtime state.

## 13. Further project documentation

- [README](README.md): operating guide and intended safety model.
- [Daily review](docs/DAILY_REVIEW.md): evaluation cutoffs, proposals and promotion criteria.
- [Dashboard](docs/DASHBOARD.md): UI architecture and startup.
- [Grok supervision](docs/GROK.md): application role prompts and integration limits.
- [Design direction](docs/design/DIRECTION.md): dashboard visual rationale.
- [News decisions](docs/NEWS_DECISIONS.md): source health, storage, evidence and decision labels.
