#!/usr/bin/env python3
"""ta_assemble.py — pure wrap-up step: concatenate report .md files into one.

PURE: no claude_agent_sdk import, no LLM call. Pure IO/string. Reads the
per-phase markdown deliverables under RUN_DIR/reports/ in manifest order, joins
them under section headers (Chinese by default, --language en for English), and
writes RUN_DIR/reports/complete_report.md. Missing files are skipped silently.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# (filename, zh_header, en_header) in manifest order.
_MANIFEST: list[tuple[str, str, str]] = [
    ("market_report.md", "市场技术分析", "Market Technical Analysis"),
    ("sentiment_report.md", "社交情绪分析", "Social Sentiment Analysis"),
    ("news_report.md", "新闻分析", "News Analysis"),
    ("fundamentals_report.md", "基本面分析", "Fundamentals Analysis"),
    ("bull.md", "多头论点", "Bull Case"),
    ("bear.md", "空头论点", "Bear Case"),
    ("risk_aggressive.md", "风险评估 — 激进", "Risk Assessment — Aggressive"),
    ("risk_conservative.md", "风险评估 — 保守", "Risk Assessment — Conservative"),
    ("risk_neutral.md", "风险评估 — 中性", "Risk Assessment — Neutral"),
    ("investment_plan.md", "投资计划（研究经理）", "Investment Plan (Research Manager)"),
    ("trader_investment_plan.md", "交易员方案", "Trader Investment Plan"),
    ("final_trade_decision.md", "最终交易决策", "Final Trade Decision"),
]

_TITLE = {"zh": "完整分析报告", "en": "Complete Analysis Report"}


def assemble(run_dir: Path, language: str) -> tuple[str, int]:
    lang = "en" if language.lower().startswith("en") else "zh"
    reports_dir = run_dir / "reports"

    parts: list[str] = [f"# {_TITLE[lang]}", ""]
    included = 0
    for filename, zh_header, en_header in _MANIFEST:
        path = reports_dir / filename
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8").strip()
        if not body:
            continue
        header = en_header if lang == "en" else zh_header
        parts.append(f"## {header}")
        parts.append("")
        parts.append(body)
        parts.append("")
        included += 1

    content = "\n".join(parts).rstrip() + "\n"
    out_path = reports_dir / "complete_report.md"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return str(out_path), included


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Concatenate report .md files into complete_report.md (pure, no LLM)."
    )
    parser.add_argument("--run-dir", required=True, help="RUN_DIR for this ticker/date.")
    parser.add_argument("--language", default="zh",
                        help="Header language: zh (default) or en.")
    args = parser.parse_args(argv)

    out_path, included = assemble(Path(args.run_dir), args.language)
    print(f"wrote {out_path} ({included} sections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
