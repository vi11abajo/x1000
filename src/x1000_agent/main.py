from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

from x1000_agent.config import AgentConfig, _load_dotenv
from x1000_agent.engine import TradingEngine
from x1000_agent.ai_engine import AIEngine
from x1000_agent.ai import AIAgent
from x1000_agent.okx_cli import OkxCli
from x1000_agent.strategy import CompositeStrategy

log = logging.getLogger("x1000")


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_check(args: argparse.Namespace) -> int:
    okx = OkxCli(profile=args.profile)
    if args.check == "balance":
        data = okx.get_balance()
    elif args.check == "positions":
        data = okx.swap_positions()
    else:
        data = okx.get_ticker(args.inst_id)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = AgentConfig.from_env({"OKX_PROFILE": args.profile, "INST_ID": args.inst_id})
    _setup_logging("DEBUG" if args.verbose else "INFO")

    if args.ai:
        # AI-driven mode using Claude via AWstore
        env = _load_dotenv()
        api_key = env.get("AWSTORE_API_KEY", "")
        if not api_key:
            log.error("AWSTORE_API_KEY not set in .env")
            return 1

        ai = AIAgent(api_key=api_key, model=args.model)
        engine = AIEngine(config, ai)
        log.info("AI-driven mode enabled (model=%s)", args.model)
    else:
        # Rule-based mode (legacy strategies)
        okx = OkxCli(profile=args.profile)
        size_usd = float(config.risk.max_position_usd)
        strategy = CompositeStrategy(okx, args.inst_id, size_usd)
        engine = TradingEngine(config, strategy)
        log.info("Rule-based mode enabled (strategies: S6, S2, S1)")

    def _stop(sig, frame):
        log.info("Received %s — stopping", sig)
        engine.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if args.once:
        engine.run_once()
    else:
        try:
            engine.run_loop()
        except KeyboardInterrupt:
            engine.stop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="x1000 OKX Trading Agent")
    ap.add_argument("--profile", default="live")
    ap.add_argument("--inst-id", default="BTC-USDT-SWAP")
    ap.add_argument("-v", "--verbose", action="store_true")

    sub = ap.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Quick check (ticker/balance/positions)")
    p_check.add_argument("--check", choices=["ticker", "balance", "positions"], default="ticker")

    p_run = sub.add_parser("run", help="Start trading loop")
    p_run.add_argument("--once", action="store_true", help="Run once and exit")
    p_run.add_argument("--ai", action="store_true", help="Use AI-driven mode (Claude via AWstore)")
    p_run.add_argument("--model", default="claude-sonnet-4-20250514", help="AI model to use")

    args = ap.parse_args()

    if args.command == "check":
        return cmd_check(args)
    elif args.command == "run":
        return cmd_run(args)
    else:
        ap.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
