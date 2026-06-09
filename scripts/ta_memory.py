#!/usr/bin/env python3
"""ta_memory.py — pure post-run step: append a decision to the shared memory log.

SELF-CONTAINED: depends only on the Python stdlib. No LLM/SDK imports. The
markdown-block-per-run log (one block per run, blocks separated by '---') is
written by inlined, pure helpers (no external package on the import path).

Usage:
  python ta_memory.py append --ticker T --date D --rating R \
      (--summary-file path/to/final_trade_decision.md | --summary TEXT) \
      [--price-target X] [--time-horizon H]
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

# Memory-log separator (must match ta_context.py).
_SEPARATOR = "\n---\n"


def _memory_log_path() -> str:
    """Resolve the shared memory-log path (env override or default)."""
    return os.environ.get("TRADING_ANALYSIS_MEMORY") or os.path.expanduser(
        "~/.trading-analysis/memory.md"
    )


# --- inlined memory helpers (pure, stdlib-only) -----------------------------


def _block(ticker: str, trade_date: str, rating: str,
           price_target, time_horizon, summary: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [
        f"## {ticker} -- {trade_date}",
        f"- recorded_at: {ts}",
        f"- rating: {rating}",
    ]
    if price_target is not None:
        parts.append(f"- price_target: {price_target}")
    if time_horizon:
        parts.append(f"- time_horizon: {time_horizon}")
    parts.append("")
    parts.append(summary.strip())
    return "\n".join(parts)


def _read_blocks(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [b.strip() for b in text.split(_SEPARATOR) if b.strip()]


def _write_blocks(path: Path, blocks: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SEPARATOR.join(blocks) + "\n", encoding="utf-8")


def append_outcome(path: str, *, ticker: str, trade_date: str, rating: str,
                   price_target, time_horizon, summary: str) -> None:
    p = Path(path)
    blocks = _read_blocks(p)
    blocks.append(_block(ticker, trade_date, rating, price_target,
                         time_horizon, summary))
    _write_blocks(p, blocks)


# ----------------------------------------------------------------------------


def _cmd_append(args: argparse.Namespace) -> int:
    if args.summary_file:
        summary = Path(args.summary_file).read_text(encoding="utf-8")
    else:
        summary = args.summary or ""

    path = _memory_log_path()
    # append_outcome creates parent dirs via _write_blocks; ensure anyway.
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    append_outcome(
        path,
        ticker=args.ticker.strip(),
        trade_date=args.date,
        rating=args.rating,
        price_target=args.price_target,
        time_horizon=args.time_horizon,
        summary=summary,
    )
    print(f"appended decision for {args.ticker} -- {args.date} to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Append a trading decision to the shared memory log (pure, no LLM)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append", help="Append one decision block.")
    p_append.add_argument("--ticker", required=True)
    p_append.add_argument("--date", required=True, help="Trade date (YYYY-MM-DD).")
    p_append.add_argument("--rating", required=True, help="Final rating/decision.")
    g = p_append.add_mutually_exclusive_group(required=True)
    g.add_argument("--summary-file", dest="summary_file",
                   help="Path to a file whose contents become the block summary.")
    g.add_argument("--summary", help="Inline summary text.")
    p_append.add_argument("--price-target", dest="price_target", default=None,
                          help="Optional price target (omitted from block if absent).")
    p_append.add_argument("--time-horizon", dest="time_horizon", default=None,
                          help="Optional time horizon (omitted from block if absent).")
    p_append.set_defaults(func=_cmd_append)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
