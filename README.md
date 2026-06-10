# trading-analysis-skill

A **multi-agent single-stock analysis pipeline, packaged as a Claude Code skill**. It
runs as subagents *inside your Claude Code session*, so it needs **no separate LLM API
key** and adds **no extra per-token bill on top of the Claude Code access you already
have** — whether that access is a Pro/Max subscription or an API key. (If you're on a
subscription, that means no extra cost at all.) No Claude Agent SDK. The bundled
scripts are pure data/IO (yfinance) and contain **zero LLM calls** — so they're
portable, even though the orchestration (`SKILL.md`) targets the Claude Code harness.

Give it a ticker; it runs a 12-role research workflow and returns an actionable
decision — **BUY / SELL / HOLD** with entry tranches, stop, hedge, position sizing,
price target, and time horizon.

```
/trading-analysis NVDA
```

---

## Source & attribution

This skill is a **pure-skill reimplementation derived from**
[**TauricResearch/TradingAgents**](https://github.com/TauricResearch/TradingAgents)
(Apache-2.0) — the open-source Multi-Agents LLM Financial Trading Framework. The
pipeline design (four analysts → bull/bear debate → three-way risk committee →
research manager → trader → portfolio manager) and the role prompts are **adapted
from that project**.

The difference: upstream TradingAgents runs as a Python app that calls an LLM **API
endpoint** configured with its **own separate, metered API key** (OpenAI / Anthropic /
…). If you already use Claude Code, running it means standing up and paying for a
*second* key. This repository re-expresses the same pipeline as a **Claude Code skill**
that the host agent executes by spawning **subagents** (the `Agent` tool), so it reuses
your existing Claude Code session's model access instead of a second key — which, for
subscription users, means no extra bill. The "run it as a Claude Code slash command"
idea was also explored by
[lucemia/trading-agents-plugin](https://github.com/lucemia/trading-agents-plugin).

The data layer here is an **independent, self-contained** reimplementation on
`yfinance` — it does not include or depend on the TradingAgents codebase. See
[`NOTICE`](./NOTICE) for full attribution. Licensed under [Apache-2.0](./LICENSE).

---

## Install

**1. Add & install the plugin** (in Claude Code):

```
/plugin marketplace add 0-Captain/trading-analysis-skill
/plugin install trading-analysis
```

(Or copy the self-contained skill folder `skills/trading-analysis/` — it bundles
`SKILL.md` + `scripts/` — into `~/.claude/skills/`, and set `TRADING_ANALYSIS_SKILL_DIR`
to that folder.)

**2. Install the data dependencies** (one-time, into whatever Python you'll point the
skill at):

```bash
pip install -r requirements.txt    # yfinance, pandas, stockstats
```

If `python3` on your PATH is not where you installed them, set
`TRADING_ANALYSIS_PYTHON=/path/to/python`.

---

## Usage

```
/trading-analysis NVDA            # today's date
/trading-analysis 0700.HK 2026-06-05
/trading-analysis BTC-USD         # crypto: fundamentals analyst auto-skipped
```

The orchestrator runs the pipeline, writes each role's report to disk, and prints a
concise decision brief (action / rating / target / horizon / entry / stop / sizing +
core reasons + report paths). Ask for "the full report" to get the assembled
`complete_report.md`.

---

## Pipeline (single round, 12 roles, each fires once)

```
Phase 1  (parallel)   market · sentiment · news · fundamentals   analysts
Phase 2  (sequential) bull  →  bear (rebuts bull)
Phase 3  (parallel)   risk committee: aggressive · conservative · neutral
Phase 4               research manager   → investment_plan + research_plan.json
Phase 5               trader             → trade plan + trader_proposal.json
Phase 6               portfolio manager  → final decision + portfolio_decision.json
wrap-up               assemble complete report + append decision to memory
```

### How it works (and why it stays cheap on context)

- The host agent is the **orchestrator**. Each role is a **subagent** (`Agent` tool) —
  so all the LLM work runs on your existing Claude Code session (no separate API key).
- **Self-contained skill layout**: `skills/trading-analysis/` bundles `SKILL.md` (the
  orchestration spec), `scripts/` (pure data/IO), and `references/` — one file per role
  holding that role's verbatim system prompt. `SKILL.md` stays lean and points at the
  reference files; the orchestrator reads a role's prompt only when it dispatches that role.
- **Disk-based handoff**: every subagent writes its full report to
  `RUN_DIR/reports/*.md` and returns **only a one-line status**. The orchestrator
  passes downstream subagents the **file paths** to read — never pasted report
  bodies — so its context never balloons across 12 roles. A report file existing on
  disk also doubles as the resume/skip marker.
- **Scripts are pure data/IO**: `ta_data.py` (yfinance market data, with a
  `get_verified_snapshot` ground-truth guard so analysts never invent prices),
  `ta_parse.py` (validates the structured JSON sidecars), `ta_context.py` (asset-type
  detection + instrument preamble + memory load), `ta_memory.py` (append decision),
  `ta_assemble.py` (combine reports). None import an LLM or SDK.
- **Cross-run memory**: each decision is appended to a memory file; the portfolio
  manager reads recent history on the next run for the same ticker.

---

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `CLAUDE_PLUGIN_ROOT` | Plugin root (set automatically when installed via `/plugin`) | — |
| `TRADING_ANALYSIS_SKILL_DIR` | The skill folder (`skills/trading-analysis`), if not installed as a plugin | — |
| `TRADING_ANALYSIS_PYTHON` | Interpreter with the data deps | `python3` |
| `TRADING_ANALYSIS_RUNS_DIR` | Where run artifacts are written | `~/.trading-analysis/runs` |
| `TRADING_ANALYSIS_MEMORY` | Cross-run decision memory file | `~/.trading-analysis/memory.md` |

Nothing is hardcoded to any machine path.

---

## Data source & caveats

- Market data, fundamentals, statements, news, and insider transactions come from
  **yfinance** (Yahoo Finance). Coverage and timeliness vary by market; non-US
  tickers and some fields can be sparse.
- `get_verified_snapshot` returns the latest **settled** OHLCV row on or before the
  requested date plus computed indicators, and instructs agents to treat it as the
  source of truth — but "today's" bar can still drift intraday until the close
  settles, so a same-day run may anchor to a provisional price.
- On any data error the scripts emit a `NO_VERIFIED_DATA:` sentinel and exit cleanly
  rather than letting an agent fabricate numbers.

## Disclaimer

This software is for research and educational purposes only. It is **not financial
advice**. Markets are risky; do your own due diligence. The authors and contributors
accept no liability for any use of this software or its output.

## License

[Apache-2.0](./LICENSE). Derived from TradingAgents (Apache-2.0); see [`NOTICE`](./NOTICE).
