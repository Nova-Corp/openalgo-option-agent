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

## 2. Daily use

| Command | What it does |
|---|---|
| `./run.sh check` | Preflight: ping, mode, funds, resolves today's ATM CE/PE, pulls candles, tests websocket |
| `./run.sh run` | Runs the trading loop for today (start any time before 15:10; it waits for 09:15) |
| `./run.sh run --forever` | Keeps running: waits for the next session (skips nights, weekends, holidays), trades, repeats. Stops when a `STOP` file appears |
| `./run.sh status` | Mode, funds, today's trades from the journal, open positions in OpenAlgo |
| `./run.sh squareoff` | Exits the agent's open trade now (`--all` also runs OpenAlgo *closeposition*) |
| `./run.sh backtest --days 20` | Replays the signal on history with a premium proxy (see §6) |
| `./run.sh journal` | Prints closed trades from `state/trades.csv` with total P&L |
| `./run.sh dashboard` | Web UI at http://127.0.0.1:8787 : current position with live P&L, trade history with filters, auto refresh |
| `./run.sh mode` | Shows analyzer state; `--paper` switches analyzer ON (safe direction only) |

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

## 3. The strategy (`orb_trend`)

Evaluated on every closed 5-minute candle of the underlying index:

1. **Opening range** = high/low of 09:15–09:30.
2. **CE entry**: a candle closes above the range high, close > VWAP, EMA9 > EMA21.
   **PE entry**: mirror image below the range low.
3. Filters: range width between 0.10 % and 0.80 % of price; the breakout close must not be more than
   0.30 % beyond the range (no chasing); entries only 09:30–14:30; max 2 trades/day, 1 per direction;
   10-minute cooldown after an exit.
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

## 4. Reliability features

- **Mode latch**: refuses to run live unless analyzer is OFF *and* `allow_live: true`; re-checks mode every minute and squares off if it flips to live without permission.
- **Fill confirmation**: every order is polled via `orderstatus`; on timeout the position is checked, unconfirmed orders are cancelled before anything is re-sent, so no duplicate exits.
- **Exit retries** then `closeposition` as a last resort, with a loud log/alert if even that fails.
- **Crash-safe journal**: state is written after every transition. On restart the open trade is resumed (stop levels included) or closed out if it was exited in the OpenAlgo UI.
- **Feed fallback**: websocket LTP with automatic REST `quotes` polling when the socket is stale.
- **No retries on unknown order outcomes** (only on 429 rate limits), so a timeout never doubles an order.
- Trading-day check via OpenAlgo holidays; unmanaged positions in the same underlying are logged, never touched.

## 5. Going live (when you are ready)

1. Paper trade for at least a few weeks; review `./run.sh journal` and `logs/agent.log`.
2. In OpenAlgo: turn **Analyzer OFF**.
3. In `config.yaml`: `allow_live: true`, confirm `live_max_lots: 1`, `capital`, `max_daily_loss_pct`.
4. `./run.sh check` → should print `LIVE (analyzer OFF); config allow_live=True`.
5. `./run.sh run`. The log banner prints `*** LIVE MODE ***`.

To go back to paper: turn Analyzer ON (or `./run.sh mode --paper`). `allow_live` can stay true;
the analyzer toggle alone decides whether orders are simulated.

## 6. Backtest caveat

`backtest` replays the *signal* on real index candles, but the option premium is a **proxy**
(entry premium = 0.35 % of index, moves with delta 0.5). It ignores theta decay and IV changes.
Use it to compare rules, trade counts and hit rates, not as a P&L forecast. Judge the real
behaviour from paper-trading results in `state/trades.csv`.

## 7. Tests

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
