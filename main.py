#!/usr/bin/env python3
"""Run all configured strategies once; schedule this command for repeated cycles."""
import argparse
import logging
from pathlib import Path
from dhan_algo.config import load_config
from dhan_algo.runner import load_strategies, run_strategies


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategies", type=Path, default=Path(__file__).parent / "config/strategies.json")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dhanhq import DhanContext, dhanhq
        config = load_config()
        strategies = load_strategies(args.strategies, config)
        client = dhanhq(DhanContext(config.client_id, config.access_token))
        return run_strategies(client, strategies)
    except Exception:
        logging.getLogger(__name__).exception("Strategy run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
