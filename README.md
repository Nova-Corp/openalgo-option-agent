# Intraday Option-Buying Agent for OpenAlgo

A small, reliable trading agent that buys NIFTY (or BANKNIFTY / SENSEX) weekly options
intraday through your local **OpenAlgo** desktop app and the AliceBlue broker.

- **Paper first.** While OpenAlgo's *Analyzer mode* is ON, every order is simulated by OpenAlgo
  (sandbox funds, sandbox positions). The agent works exactly the same way; nothing reaches the broker.
- **Live when you decide.** Turn Analyzer OFF in OpenAlgo **and** set `allow_live: true` in
  `config.yaml`. Both are required. If only one is set, the agent refuses to trade and tells you why.

## 1. One-time setup

```bash
cd ~/Documents/Trade
cp .env.example .env          # then paste your OpenAlgo API key into .env
./run.sh check                # verifies connection, mode, funds, contracts, candles, websocket
```

Your API key is in the OpenAlgo desktop app under **API Key** in the left menu.
The virtual environment (`.venv`, Python 3.12) and dependencies are already installed.

## 2. Every trading day

```bash
cd ~/Documents/Trade && ./morning.sh
```

Run this once in the morning. It opens OpenAlgo if needed, starts one agent per index plus the
dashboard, waits until you have logged in to AliceBlue inside OpenAlgo, then opens the dashboard.
That is the whole routine; each agent trades from 09:15, squares off at 15:10 and sleeps until the
next session.

**Is it running? Stop it.**

```bash
./run.sh status      # each index: running/stopped, today's trades, P&L; or look at the dashboard header
./stop.sh            # square off any open trade, then stop every agent (dashboard keeps running)
./stop.sh --keep     # stop the agents but leave open trades in OpenAlgo
./stop.sh --all      # also stop the dashboard
```

`./morning.sh` starts them again. Stopping and starting between sessions is harmless; the agents are
idle outside market hours anyway.

**Choosing indices.** With no arguments it starts the list in `active_underlyings` in `config.yaml`.
Name them to choose for the day:

```bash
./morning.sh NIFTY BANKNIFTY SENSEX
```

Each index is an independent agent with its own journal in `state/<INDEX>/`, its own lock, its own
lots and its own daily loss limit, so three indices means up to three times the risk of one. Agents
for indices you leave out are stopped, unless they have an open trade. Per-index settings live under
`underlyings:` in `config.yaml` (BANKNIFTY and SENSEX get a wider premium window by default). Any
command accepts `-u INDEX`, e.g. `./run.sh check -u SENSEX`, `./run.sh squareoff -u BANKNIFTY`.

SENSEX note: AliceBlue's BSE contract master uses its own symbol style (`SENSEX29thOCT84000CE`), which
OpenAlgo's option resolver does not understand. The agent detects that and resolves the contract itself
through OpenAlgo's search endpoint, so SENSEX works end to end.

## 3. Daily use

| Command | What it does |
|---|---|
| `./run.sh check` | Preflight: ping, mode, funds, resolves today's ATM CE/PE, pulls candles, tests websocket |
| `./run.sh run` | Runs the trading loop for today (start any time before 15:10; it waits for 09:15) |
| `./run.sh run --forever` | Keeps running: waits for the next session (skips nights, weekends, holidays), trades, repeats. Stops when a `STOP` file appears |
| `./run.sh status` | Mode, funds, today's trades from the journal, open positions in OpenAlgo |
| `./run.sh squareoff` | Exits the agent's open trade now (`--all` also runs OpenAlgo *closeposition*) |
| `./run.sh backtest --days 20` | Replays the signal on history with a premium proxy (see §6) |
| `./run.sh journal` | Prints closed trades from `state/trades.csv` with total P&L |
| `./run.sh dashboard` | Web UI at http://127.0.0.1:8787 : current positions with live P&L, trade charts, trade history with filters, auto refresh |
| `./run.sh mode` | Shows analyzer state; `--paper` switches analyzer ON (safe direction only) |
| `./run.sh ping` | Exit 0 when OpenAlgo has a broker session (used by morning.sh) |

**Every morning before 09:15, log in to AliceBlue inside the OpenAlgo app.** Broker sessions expire
daily; the agent pings OpenAlgo from 09:05 and logs "OpenAlgo not ready ... Log in to the broker"
every 30 s until the session is back. Keep the OpenAlgo app and this Mac awake (the recommended
launch is `caffeinate -i ./run.sh run --forever`, which stops idle sleep while it runs).

Controls while it runs (no restart needed):

- create a file named `PAUSE` in this folder → no new entries, open trade still managed
- create a file named `STOP` → open trade is squared off and the agent exits
- `Ctrl+C` → the agent stops **without** closing the position; run `./run.sh squareoff`

Logs: console plus `logs/agent.log`. State: `state/<date>.json` (today's trades, stop levels) and
`state/trades.csv` (all closed trades).

## 3b. Trade charts

The dashboard's "Trade chart" section draws, for any index and day:

- the underlying's 5-minute candles with EMA fast/slow, VWAP, the opening range (dashed band from
  09:30 on), a ▲/▼ on the breakout candle, ● at entry and ■ at exit with the P&L;
- the option premium of the selected trade with the entry line, the target line and the stop as a
  stepped line that rises when breakeven and trailing kick in.

Premium data comes from `state/<INDEX>/ticks-<trade id>.csv`, which the agent writes once per second
while a trade is open (from 28 Oct 2026 on). For trades without a tick file the chart falls back to the
option's 1-minute history from OpenAlgo while the contract is alive. The chart library (TradingView
Lightweight Charts, Apache-2.0) is bundled in `agent/static/`, so the page works offline.

## 4. The strategy (`orb_trend`)

Evaluated on every closed 5-minute candle of the underlying index:

1. **Opening range** = high/low of 09:15–09:30.
2. **CE entry**: a candle closes above the range high, close > VWAP, EMA9 > EMA21.
   **PE entry**: mirror image below the range low.
3. Filters: range width between 0.10 % and 0.80 % of price; the breakout close must not be more than
   0.30 % beyond the range (no chasing); entries only 09:30–14:30; max 2 trades/day, 1 per direction;
   10-minute cooldown after an exit.
   **Candle quality**: the breakout candle must be decisive, not a doji: body at least 30 % of its
   high-low range, coloured in the breakout direction (green for CE, red for PE), and closing in the
   top third (CE) / bottom third (PE) of the candle. An indecisive breakout candle is skipped and the
   next candle is judged by the same rules, so a reversal off a doji is never bought.
4. Buys the **ATM** option of the **nearest expiry** (configurable: OTM1/ITM1, `next` expiry,
   `min_days_to_expiry: 1` to skip expiry day).

Exit rules, checked on every tick of the option price:

- stop-loss 25 % below entry premium, target +50 %
- stop moves to breakeven at +15 %, trails 12 % below the highest premium once +25 %
- underlying exit: a 5-minute close back through VWAP / EMA21 against the trade
- time exit at **15:10**, daily loss limit 2 % of capital stops trading for the day

Sizing: lots = risk-per-trade ÷ (premium × stop % × lot size), clamped to `min_lots..max_lots`,
and additionally capped by `live_max_lots` when live. A premium whose single-lot risk exceeds twice
the budget is skipped.

All numbers live in `config.yaml`. The index has no volume, so VWAP falls back to a
time-weighted average of typical price; set `signal_symbol` to the current-month future for a true VWAP.

## 4b. Candles when the broker's history fails

AliceBlue's history API sometimes stops serving a day's intraday candles for a while (seen with
SENSEX on 28 Oct 2026). Each agent therefore samples its index's live price every second and builds its
own 5-minute candles from those ticks (`state/<INDEX>/tickcandles-<date>.csv`). When the broker has
not published a candle `candle_grace_sec` (20 s) after it closed, the agent uses the tick-built candle
instead, and fills any earlier gaps the same way; broker candles always win when both exist. The chart
does the same merge, and says so under the chart. Tick candles carry no volume and, if the agent started
mid-candle, the first one is marked partial.

## 5. Reliability features

- **Mode latch**: refuses to run live unless analyzer is OFF *and* `allow_live: true`; re-checks mode every minute and squares off if it flips to live without permission.
- **Fill confirmation**: every order is polled via `orderstatus`; on timeout the position is checked, unconfirmed orders are cancelled before anything is re-sent, so no duplicate exits.
- **Exit retries** then `closeposition` as a last resort, with a loud log/alert if even that fails.
- **Crash-safe journal**: state is written after every transition. On restart the open trade is resumed (stop levels included) or closed out if it was exited in the OpenAlgo UI.
- **Feed fallback**: websocket LTP with automatic REST `quotes` polling when the socket is stale.
- **No retries on unknown order outcomes** (only on 429 rate limits), so a timeout never doubles an order.
- Trading-day check via OpenAlgo holidays; unmanaged positions in the same underlying are logged, never touched.

## 6. Going live (when you are ready)

1. Paper trade for at least a few weeks; review `./run.sh journal` and `logs/agent.log`.
2. In OpenAlgo: turn **Analyzer OFF**.
3. In `config.yaml`: `allow_live: true`, confirm `live_max_lots: 1`, `capital`, `max_daily_loss_pct`.
4. `./run.sh check` → should print `LIVE (analyzer OFF); config allow_live=True`.
5. `./run.sh run`. The log banner prints `*** LIVE MODE ***`.

To go back to paper: turn Analyzer ON (or `./run.sh mode --paper`). `allow_live` can stay true;
the analyzer toggle alone decides whether orders are simulated.

## 7. Backtest caveat

`backtest` replays the *signal* on real index candles, but the option premium is a **proxy**
(entry premium = 0.35 % of index, moves with delta 0.5). It ignores theta decay and IV changes.
Use it to compare rules, trade counts and hit rates, not as a P&L forecast. Judge the real
behaviour from paper-trading results in `state/trades.csv`.

## 8. Tests

```bash
source .venv/bin/activate && python -m pytest -q tests
```

Covers expiry selection, sizing, stop logic, the signal rules, and full simulated days through the
engine with a fake broker: target hit, stop-loss, rejected entry, live-mode refusal and lot cap, restart
resume, external close, STOP file, and an exit order that times out.

## Layout

```
agent/          config, clock, broker (OpenAlgo wrapper), instruments, data (candles+LTP feed),
                strategy, risk, execution, journal, engine, backtest, notify, __main__ (CLI)
tests/          fake_broker.py + unit/engine tests
config.yaml     all strategy / risk / execution parameters
.env            OPENALGO_API_KEY (never commit)
state/, logs/   journal and logs (created automatically)
```

*This software places orders automatically. Option buying can lose the full premium. Nothing here is
investment advice; the default parameters are a starting point for your own paper testing.*
