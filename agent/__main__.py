"""CLI: python -m agent <check|run|status|squareoff|mode|backtest|journal>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich import print as rprint
from rich.console import Console
from rich.table import Table

from .config import load_config
from .notify import setup_logging

console = Console()


def _broker(cfg):
    from .broker import OpenAlgoBroker

    return OpenAlgoBroker(cfg.openalgo, cfg.execution.strategy_tag)


def cmd_check(cfg, args):
    from .engine import Engine

    eng = Engine(cfg, _broker(cfg))
    res = eng.preflight()
    t = Table(title="OpenAlgo preflight", show_lines=False)
    t.add_column("check")
    t.add_column("result")
    t.add_row("ping", json.dumps(res["ping"]))
    mode = "PAPER (analyzer ON)" if res["paper_mode"] else "LIVE (analyzer OFF)"
    t.add_row("mode", f"{mode}; config allow_live={res['allow_live']}")
    f = res["funds"]
    t.add_row("funds", f"available={f.get('availablecash')} used={f.get('utiliseddebits')}")
    for ot in ("CE", "PE"):
        c = res[f"contract_{ot}"]
        t.add_row(f"{ot} contract", f"{c['symbol']} lot={c['lotsize']} expiry={c['expiry_date']} ltp={c['ltp']} underlying={c['underlying_ltp']}")
    c = res["candles"]
    t.add_row("candles", f"{c['exchange']}:{c['symbol']} rows={c['rows']} last={c['last']}")
    t.add_row("holidays", str(res["holidays_loaded"]))
    t.add_row("websocket", "connected" if res["ws_connected"] else "NOT connected (REST fallback will be used)")
    console.print(t)
    if not res["paper_mode"] and not res["allow_live"]:
        rprint("[bold red]Analyzer is OFF but allow_live is false: `run` will refuse to trade.[/bold red]")
    return 0


def cmd_run(cfg, args):
    from .engine import Engine, ModeNotAllowed

    eng = Engine(cfg, _broker(cfg))
    try:
        if args.forever:
            eng.run_forever()
        else:
            eng.run()
    except ModeNotAllowed as e:
        rprint(f"[bold red]{e}[/bold red]")
        return 2
    except KeyboardInterrupt:
        rprint("[yellow]interrupted; open position (if any) is still live in OpenAlgo. Use `squareoff` to flatten.[/yellow]")
        return 130
    return 0


def cmd_status(cfg, args):
    from .clock import now_ist
    from .journal import Journal

    b = _broker(cfg)
    paper = b.analyzer_mode()
    rprint(f"mode: {'PAPER (analyzer ON)' if paper else '[bold red]LIVE (analyzer OFF)[/bold red]'}")
    rprint("funds:", b.funds())
    j = Journal(Path(cfg.project_dir) / cfg.state_dir, now_ist().date())
    t = Table(title=f"today's trades ({j.day})")
    for col in ("status", "symbol", "qty", "entry", "exit", "pnl", "reason"):
        t.add_column(col)
    for tr in j.trades:
        t.add_row(tr.status, tr.symbol, str(tr.qty), f"{tr.entry_price:.2f}", f"{tr.exit_price:.2f}", f"{tr.pnl:+.2f}", tr.exit_reason[:50])
    console.print(t)
    rprint(f"realized pnl today: {j.realized_pnl():+.2f}")
    pos = [p for p in b.positionbook() if int(float(p.get("quantity", 0) or 0)) != 0]
    rprint("open positions in OpenAlgo:", pos or "none")
    return 0


def cmd_squareoff(cfg, args):
    from .clock import now_ist
    from .execution import Executor
    from .journal import Journal
    from .clock import Clock

    b = _broker(cfg)
    j = Journal(Path(cfg.project_dir) / cfg.state_dir, now_ist().date())
    t = j.open_trade()
    if t:
        ex = Executor(b, cfg.execution, Clock())
        ltp = b.quotes(t.symbol, t.exchange).get("ltp") or t.entry_price
        ex.exit(t, "manual squareoff", ltp)
        j.update(t)
        rprint(f"closed {t.symbol} @ {t.exit_price:.2f} pnl={t.pnl:+.2f}")
    else:
        rprint("no open trade in the journal.")
    if args.all:
        rprint("closeposition:", b.close_all_positions())
    return 0


def cmd_mode(cfg, args):
    b = _broker(cfg)
    if args.paper:
        b.api.analyzertoggle(mode=True)
    paper = b.analyzer_mode()
    rprint(f"OpenAlgo analyzer: {'ON -> paper trading' if paper else 'OFF -> LIVE trading'}")
    rprint(f"config allow_live: {cfg.execution.allow_live}  (live orders happen only when analyzer is OFF AND allow_live is true)")
    return 0


def cmd_backtest(cfg, args):
    from .backtest import run_backtest, summarize

    trades, df = run_backtest(cfg, _broker(cfg), days=args.days, proxy_premium_pct=args.premium_pct,
                              delta=args.delta, lotsize=args.lotsize)
    t = Table(title=f"backtest {cfg.signal_symbol} {cfg.strategy.candle_interval} last {args.days} days (premium PROXY)")
    for col in ("date", "type", "in", "out", "idx in", "idx out", "prem in", "prem out", "qty", "pnl", "exit"):
        t.add_column(col)
    for x in trades:
        t.add_row(x.date, x.option_type, x.entry_time, x.exit_time, f"{x.index_entry:.0f}", f"{x.index_exit:.0f}",
                  f"{x.premium_entry:.1f}", f"{x.premium_exit:.1f}", str(x.qty), f"{x.pnl:+.0f}", x.reason)
    console.print(t)
    rprint(summarize(trades))
    out = Path(cfg.project_dir) / cfg.state_dir / "backtest_last.csv"
    import pandas as pd

    pd.DataFrame([x.__dict__ for x in trades]).to_csv(out, index=False)
    rprint(f"saved {out}")
    return 0


def cmd_dashboard(cfg, args):
    from .dashboard import serve

    rprint(f"dashboard: http://{args.host}:{args.port}/")
    serve(cfg, _broker(cfg), host=args.host, port=args.port)
    return 0


def cmd_journal(cfg, args):
    p = Path(cfg.project_dir) / cfg.state_dir / "trades.csv"
    if not p.exists():
        rprint("no closed trades yet.")
        return 0
    import pandas as pd

    df = pd.read_csv(p)
    console.print(df.tail(args.n).to_string())
    rprint(f"total pnl: {df['pnl'].sum():+.2f} over {len(df)} trades")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent", description="Intraday option-buying agent for OpenAlgo")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify OpenAlgo connectivity, mode, funds, contracts, candles")
    rn = sub.add_parser("run", help="run the trading loop for today")
    rn.add_argument("--forever", action="store_true", help="keep running: wait for the next session, trade, repeat")
    sub.add_parser("status", help="mode, funds, today's trades, open positions")
    sq = sub.add_parser("squareoff", help="exit the journal's open trade now")
    sq.add_argument("--all", action="store_true", help="also call OpenAlgo closeposition for every position")
    md = sub.add_parser("mode", help="show analyzer mode; --paper switches analyzer ON")
    md.add_argument("--paper", action="store_true")
    bt = sub.add_parser("backtest", help="replay the signal on historical candles (premium proxy)")
    bt.add_argument("--days", type=int, default=20)
    bt.add_argument("--premium-pct", type=float, default=0.35)
    bt.add_argument("--delta", type=float, default=0.5)
    bt.add_argument("--lotsize", type=int, default=65)
    db = sub.add_parser("dashboard", help="local web UI: position, trades, P&L (auto-refresh)")
    db.add_argument("--port", type=int, default=8787)
    db.add_argument("--host", default="127.0.0.1")
    jn = sub.add_parser("journal", help="show closed trades")
    jn.add_argument("-n", type=int, default=30)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(Path(cfg.project_dir) / cfg.log_dir, args.log_level)
    from .broker import BrokerError

    try:
        return {"check": cmd_check, "run": cmd_run, "status": cmd_status, "squareoff": cmd_squareoff,
                "mode": cmd_mode, "backtest": cmd_backtest, "journal": cmd_journal,
                "dashboard": cmd_dashboard}[args.cmd](cfg, args)
    except BrokerError as e:
        rprint(f"[bold red]OpenAlgo error:[/bold red] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
