#!/usr/bin/env python3
"""ta_context.py — pure bootstrap step for the trading-analysis skill.

Writes ``RUN_DIR/state/context.json`` with:
  {ticker, date, asset_type, analysts, instrument_preamble, past_memory}
and prints the ``instrument_preamble`` to stdout so the orchestrator can inject
it into every subagent prompt.

SELF-CONTAINED: depends only on the Python stdlib. No LLM/SDK imports, no
network. The pure helpers it needs (asset-type detection, analyst filtering,
instrument-preamble rendering, and past-memory lookup) are inlined below so the
script runs from anywhere without any repo on the import path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

# Canonical analyst roster for the single-round pipeline (crypto drops
# fundamentals via filter_analysts_for_asset_type).
_DEFAULT_ANALYSTS = ["market", "sentiment", "news", "fundamentals"]

# Crypto ticker suffixes — keep in sync with upstream cli.utils.CRYPTO_SUFFIXES.
_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")

# Memory-log separator (must match ta_memory.py).
_SEPARATOR = "\n---\n"


def _memory_log_path() -> str:
    """Resolve the shared memory-log path (env override or default)."""
    return os.environ.get("TRADING_ANALYSIS_MEMORY") or os.path.expanduser(
        "~/.trading-analysis/memory.md"
    )


# --- inlined instrument helpers (pure, stdlib-only) -------------------------


def detect_asset_type(ticker: str) -> str:
    """Return "crypto" for crypto tickers, "stock" otherwise."""
    if ticker.strip().upper().endswith(_CRYPTO_SUFFIXES):
        return "crypto"
    return "stock"


def filter_analysts_for_asset_type(analysts: list[str], asset_type: str) -> list[str]:
    """Drop analysts that don't apply to the asset type (crypto: no fundamentals)."""
    if asset_type != "crypto":
        return analysts
    return [a for a in analysts if a != "fundamentals"]


def build_instrument_preamble(ticker: str, asset_type: str) -> str:
    """Inlined, pure-string copy of agent_utils.build_instrument_context.

    Rendered with empty identity (no network) so the bootstrap step is
    offline-deterministic. Still emits the full anti-hallucination preamble plus
    the crypto note.
    """
    is_crypto = asset_type == "crypto"
    instrument_label = "asset" if is_crypto else "instrument"
    context = (
        f"The {instrument_label} to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`, `-USD`)."
    )
    if is_crypto:
        context += (
            " Treat it as a crypto asset rather than a company, and do not "
            "assume company fundamentals are available."
        )
    return context


# --- inlined memory helpers (pure, stdlib-only) -----------------------------


def _read_blocks(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [b.strip() for b in text.split(_SEPARATOR) if b.strip()]


def load_past_context(ticker: str, path: str, *, max_entries: int = 5) -> str:
    """Return the last few memory blocks whose heading matches ``ticker``."""
    p = Path(path)
    pattern = re.compile(rf"^##\s+{re.escape(ticker)}\b", re.MULTILINE)
    matches = [b for b in _read_blocks(p) if pattern.search(b)]
    return "\n\n---\n\n".join(matches[-max_entries:])


# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap trading-analysis run state (pure, no LLM)."
    )
    parser.add_argument("--ticker", required=True, help="Ticker symbol.")
    parser.add_argument("--date", required=True, help="Trade date (YYYY-MM-DD).")
    parser.add_argument(
        "--run-dir", required=True, help="RUN_DIR for this ticker/date."
    )
    args = parser.parse_args(argv)

    ticker = args.ticker.strip()

    asset_type = detect_asset_type(ticker)
    analysts = filter_analysts_for_asset_type(list(_DEFAULT_ANALYSTS), asset_type)
    instrument_preamble = build_instrument_preamble(ticker, asset_type)

    try:
        past_memory = load_past_context(ticker, _memory_log_path())
    except Exception:  # noqa: BLE001 — never block bootstrap on memory read
        past_memory = ""

    context = {
        "ticker": ticker,
        "date": args.date,
        "asset_type": asset_type,
        "analysts": analysts,
        "instrument_preamble": instrument_preamble,
        "past_memory": past_memory or "",
    }

    state_dir = Path(args.run_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "context.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Stdout = the preamble verbatim, for orchestrator injection.
    print(instrument_preamble)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
