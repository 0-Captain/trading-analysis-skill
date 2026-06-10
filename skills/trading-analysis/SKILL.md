---
name: trading-analysis
description: >-
  Single-stock deep analysis pipeline. Runs a 12-role research workflow over ONE
  ticker (4 analysts — market/sentiment/news/fundamentals — then a bull/bear
  debate, a 3-way risk committee, a research manager, a trader, and a portfolio
  manager) and returns an actionable trade decision: BUY / SELL / HOLD with
  entry, stop-loss, position sizing, price target, and time horizon. Trigger
  this when the user wants a thorough work-up on a specific instrument and asks
  things like "analyze NVDA", "should I buy/sell/hold TSLA", "给 600276 跑一份分析",
  "is AAPL worth entering now", "deep dive on 700.HK", "run the pipeline on
  RKLB", "给我一份 X 的研报 / 尽调". Accepts $ARGUMENTS as "<TICKER> [DATE]" where
  TICKER is the symbol and DATE is an optional YYYY-MM-DD trade date (defaults
  to today). Do NOT use for pure market/sector/macro commentary, ETF
  comparisons, or a single quote/indicator lookup — those don't need the full
  multi-agent run.
---

# trading-analysis — single-stock 12-role pipeline (SINGLE ROUND)

You are the **orchestrator**. You drive a fixed, single-pass pipeline of 12 roles
over one ticker. Each role fires **EXACTLY ONCE**. There is no multi-round debate,
no while-loop, no re-running a phase for "another opinion". The only re-dispatch
allowed is the one-time validation retry on the three structured roles (Phases 4–6).

## Iron rules (read before doing anything)

1. **Scripts contain NO LLM and NO reasoning.** Everything under
   `scripts/` is pure data/parse/IO (argparse + dataflows + JSON). ALL reasoning,
   judgment, and narrative writing happens inside **subagents** you spawn with the
   **Agent** tool. Never ask a script to "decide" anything.
2. **State lives on disk, not in your context window.** Each subagent writes its
   FULL output to a file under `RUN_DIR/reports/` (and, for structured roles, a
   sidecar under `RUN_DIR/state/`) and **returns ONLY ONE LINE**. You pass
   downstream subagents the **FILE PATHS to Read** — you NEVER paste a report body
   into a prompt. You yourself only Read the small `state/*.json` sidecars for
   control flow and the final brief.
3. **Inject the instrument preamble into EVERY subagent.** Step 0 produces a short
   `instrument_preamble` string. You must paste it into the `{instrument_preamble}`
   slot of every role's system prompt so no subagent drifts to the wrong company
   or invents prices.
4. **Proof-of-work = the file exists.** A phase is "done" only when its `.md`
   output file actually exists on disk. Treat the file's existence as proof the
   phase ran. If a subagent returns its one-line summary but the file is missing,
   the phase did NOT run — re-dispatch it.
5. **Resume.** If `RUN_DIR/reports/final_trade_decision.md` already exists, the whole
   run is complete: REUSE it, skip the entire pipeline, jump straight to the Final
   Brief. Otherwise, for each phase, if that phase's output `.md` already exists,
   SKIP that subagent and reuse the file.
6. **TodoWrite.** Create one todo per phase (Step 0 through Wrap-up) before you
   start, and mark each `in_progress` / `completed` as you go. Do not batch-complete.

---

## Path conventions

Two values are resolved once in Step 0 and reused everywhere:

- **`SKILL_DIR`** = this skill's own directory (it bundles `SKILL.md` + `scripts/`). When installed
  as a plugin, it is `$CLAUDE_PLUGIN_ROOT/skills/trading-analysis` (Claude Code sets
  `CLAUDE_PLUGIN_ROOT` automatically). If `CLAUDE_PLUGIN_ROOT` is unset (e.g. the skill folder was
  copied into `~/.claude/skills/` on its own), the user exports `TRADING_ANALYSIS_SKILL_DIR` to the
  skill folder. There is NO machine-specific fallback — Step 0 errors clearly if neither resolves.
- **`TA_PY`** = a Python interpreter with the data deps (`yfinance`, `pandas`, `stockstats`)
  installed. Defaults to `python3`, overridable via `TRADING_ANALYSIS_PYTHON`. Step 0 **preflights**
  it. EVERY python call — scripts AND the `ta_data.py` calls inside subagents — uses `TA_PY`, never
  bare `python` (a system interpreter without the deps would make `ta_data.py` return no data and
  analysts would invent numbers). Always use `TA_PY`.

Scripts (all bundled under `SKILL_DIR/scripts/`) are therefore invoked as:

```
"$TA_PY" "$SKILL_DIR/scripts/<script>.py" <args>
```

`RUN_DIR` (computed in Step 0) is:

```
${TRADING_ANALYSIS_RUNS_DIR:-$HOME/.trading-analysis/runs}/<TICKER>/<DATE>
```

All report paths below (e.g. `reports/market_report.md`) are **relative to `RUN_DIR`**.
When you spawn a subagent, tell it explicitly that its working/run directory is the
absolute `RUN_DIR`, so its relative Read/Write paths resolve there.

---

## STEP 0 — Bootstrap (Bash, NO LLM)

Parse `$ARGUMENTS` as `"<TICKER> [DATE]"`:

- `TICKER` = first whitespace-separated token, upper-cased.
- `DATE`   = second token if present and looks like `YYYY-MM-DD`; otherwise today,
  via `date +%F`.

Then run the bootstrap script and create the run directory. Do this in Bash:

```bash
# --- Resolve this skill's own directory (no machine-specific fallback) ---
# Plugin install: $CLAUDE_PLUGIN_ROOT/skills/trading-analysis. Bare skill copy: $TRADING_ANALYSIS_SKILL_DIR.
SKILL_DIR="${TRADING_ANALYSIS_SKILL_DIR:-${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/trading-analysis}}"
if [ -z "$SKILL_DIR" ] || [ ! -f "$SKILL_DIR/scripts/ta_data.py" ]; then
  echo "SKILL_DIR_UNRESOLVED: set CLAUDE_PLUGIN_ROOT (auto when installed via /plugin) or"
  echo "export TRADING_ANALYSIS_SKILL_DIR=/path/to/skills/trading-analysis"
  exit 1
fi
TICKER="<TICKER>"          # from $ARGUMENTS, upper-cased
DATE="<DATE>"              # from $ARGUMENTS, or: DATE="$(date +%F)"
RESULTS_ROOT="${TRADING_ANALYSIS_RUNS_DIR:-$HOME/.trading-analysis/runs}"
RUN_DIR="${RESULTS_ROOT}/${TICKER}/${DATE}"
mkdir -p "${RUN_DIR}/reports" "${RUN_DIR}/state"

# --- Resolve the data-capable Python interpreter (default python3, overridable) ---
TA_PY="${TRADING_ANALYSIS_PYTHON:-python3}"
# PREFLIGHT — fail LOUDLY instead of silently degrading to no-data.
if ! "$TA_PY" -c "import yfinance, pandas, stockstats" >/dev/null 2>&1; then
  echo "PREFLIGHT_FAILED: '$TA_PY' is missing the data deps (yfinance, pandas, stockstats)."
  echo "Fix: pip install yfinance pandas stockstats  (or set TRADING_ANALYSIS_PYTHON to an interpreter that has them)."
  exit 1
fi
echo "TA_PY=$TA_PY"

# Resume short-circuit: whole run already complete?
if [ -f "${RUN_DIR}/reports/final_trade_decision.md" ]; then
  echo "RESUME: final_trade_decision.md exists — skipping pipeline."
fi

# Bootstrap context (writes RUN_DIR/state/context.json). NO LLM here.
"$TA_PY" "${SKILL_DIR}/scripts/ta_context.py" --ticker "${TICKER}" --date "${DATE}" --run-dir "${RUN_DIR}"
cat "${RUN_DIR}/state/context.json"
```

**If the bash above prints `PREFLIGHT_FAILED`, STOP immediately.** Do not spawn any subagent —
without data they would invent numbers. Tell the user exactly how to fix it (the message above)
and end the run.

`ta_context.py` writes `RUN_DIR/state/context.json` with these fields:
`{ "ticker": ..., "date": ..., "asset_type": ..., "analysts": [...], "instrument_preamble": "<string>", "past_memory": "<string>" }`.

After it runs:

- **Read `RUN_DIR/state/context.json`** (this is a small sidecar — reading it is allowed).
- Capture the resolved **`TA_PY`** path (printed as `TA_PY=...`) — inject it as `{py}` into every
  subagent that calls `ta_data.py`, and use it for every orchestrator-level python call.
- Capture **`instrument_preamble`** (string) — you will inject it into every subagent.
- Capture **`analysts`** (array) — this is the **authoritative** Phase-1 role list. It already
  excludes `fundamentals` for crypto (the script applied the filter). Spawn exactly one analyst
  per entry. (`asset_type == "crypto"` is the underlying reason; `analysts` is the single source of truth.)
- Capture **`past_memory`** (string) — you will inject it into the portfolio_manager in Phase 6.

If `final_trade_decision.md` already existed above, skip to **Final Brief** now.

---

## How to dispatch a subagent (applies to every role below)

For each role you spawn ONE **Agent** tool call. The Agent prompt you send must:

1. State the absolute `RUN_DIR` and that all relative paths resolve there.
2. Contain the role's **verbatim system prompt** (reproduced below), with these
   placeholders substituted:
   - `{instrument_preamble}` → the string from `context.json`.
   - `{output_language}` → `中文` (Simplified Chinese) unless the user explicitly
     asked for another language.
   - `{past_memory}` → (portfolio_manager only) the `past_memory` string from
     `context.json`.
   - `{py}` → (analysts only) the resolved `TA_PY` interpreter path from Step 0.
   - `{skill_dir}` → (analysts only) the resolved `SKILL_DIR` from Step 0.
   Substitute `{py}` and `{skill_dir}` with their concrete absolute values before
   sending — do not leave them for the subagent's shell to expand.
3. For roles with `reads`, give the subagent the **FILE PATHS to Read** (absolute or
   `RUN_DIR`-relative). NEVER paste report bodies into the prompt.
4. Remind it: write your full output to your report file(s), then RETURN ONLY the
   single specified line and nothing else.

After each Agent returns, **verify the report file exists on disk** before moving on.

---

## STEP 1 — Phase 1: four analysts IN PARALLEL

**Spawn these analysts IN PARALLEL — in a SINGLE message with multiple Agent tool
calls** — ONE per entry in the `analysts` array from `context.json` (it is already
crypto-filtered: 4 for equities, 3 for crypto with `fundamentals` dropped). Each
analyst calls ONLY its own allowed `ta_data.py` subcommands, writes its `.md`, and
returns one line. None of them read any report (their `reads` are empty).

`ta_data.py` subcommands take **POSITIONAL** args (and a few `--underscore_flags`); they are
invoked inside the subagent via Bash as (with `{py}` / `{skill_dir}` already substituted):

```
{py} "{skill_dir}/scripts/ta_data.py" <subcommand> <positional-args> [--underscore_flags]
```

Per-analyst allowed subcommands (exact signatures shown in each role prompt below):
- **market** → `get_verified_snapshot`, `get_stock_data`, `get_indicators`
- **sentiment** → `get_news`
- **news** → `get_news`, `get_global_news`, `get_insider_transactions`
- **fundamentals** → `get_fundamentals`, `get_balance_sheet`, `get_cashflow`, `get_income_statement`
  (present in `analysts` only for non-crypto)

### 1a — market analyst → `reports/market_report.md` → `wrote reports/market_report.md | SIGNAL: <bullish|neutral|bearish>`

**System prompt:** Read `references/market_analyst.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

### 1b — sentiment analyst → `reports/sentiment_report.md` → `wrote reports/sentiment_report.md | BAND: <overall_band>`

**System prompt:** Read `references/sentiment_analyst.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

### 1c — news analyst → `reports/news_report.md` → `wrote reports/news_report.md | SIGNAL: <positive|neutral|negative>`

**System prompt:** Read `references/news_analyst.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

### 1d — fundamentals analyst → `reports/fundamentals_report.md` → `wrote reports/fundamentals_report.md | RATING: <High|Above Average|Average|Below Average|Distressed>`

**SKIP THIS ROLE ENTIRELY IF `asset_type == "crypto"`.**

**System prompt:** Read `references/fundamentals_analyst.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

**Phase 1 gate:** verify `market_report.md`, `sentiment_report.md`, `news_report.md`
exist (and `fundamentals_report.md` unless crypto). Re-dispatch any missing one before
proceeding.

---

## STEP 2 — Phase 2: bull, THEN bear (sequential)

Spawn the bull FIRST. After it returns and `bull.md` exists, spawn the bear (the bear
must Read `bull.md`). Pass each subagent the FILE PATHS of the analyst reports to Read
(the four `reports/*_report.md`, with `fundamentals_report.md` absent for crypto).

### 2a — bull → `reports/bull.md` → `wrote reports/bull.md | SIGNAL: BULL`

Reads: `reports/market_report.md`, `reports/sentiment_report.md`, `reports/news_report.md`, `reports/fundamentals_report.md` (if present).

**System prompt:** Read `references/bull_researcher.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

### 2b — bear → `reports/bear.md` → `wrote reports/bear.md | SIGNAL: BEAR`

Reads: the four analyst reports **AND** `reports/bull.md`.

**System prompt:** Read `references/bear_researcher.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

**Phase 2 gate:** verify `bull.md` and `bear.md` exist.

---

## STEP 3 — Phase 3: risk committee trio IN PARALLEL

**Spawn aggressive + conservative + neutral IN PARALLEL — a SINGLE message with 3
Agent tool calls.** They are independent (single round). Each Reads the four analyst
reports **plus** `reports/bull.md` and `reports/bear.md`. Pass all those FILE PATHS;
paste no bodies.

### 3a — risk_aggressive → `reports/risk_aggressive.md` → `wrote reports/risk_aggressive.md | STANCE: AGGRESSIVE`

**System prompt:** Read `references/risk_aggressive.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

### 3b — risk_conservative → `reports/risk_conservative.md` → `wrote reports/risk_conservative.md | STANCE: CONSERVATIVE`

**System prompt:** Read `references/risk_conservative.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

### 3c — risk_neutral → `reports/risk_neutral.md` → `wrote reports/risk_neutral.md | STANCE: NEUTRAL`

**System prompt:** Read `references/risk_neutral.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

**Phase 3 gate:** verify `risk_aggressive.md`, `risk_conservative.md`, `risk_neutral.md` exist.

---

## STEP 4 — Phase 4: research_manager (structured) + validate

Spawn ONE research_manager. It Reads `reports/bull.md`, `reports/bear.md`,
`reports/risk_aggressive.md`, `reports/risk_conservative.md`, `reports/risk_neutral.md`
(and may Read the analyst reports to verify a figure). It writes BOTH
`reports/investment_plan.md` **and** the sidecar `state/research_plan.json`.

`state/research_plan.json` must be a single valid JSON object with EXACTLY:
`recommendation` (one of `Buy`/`Overweight`/`Hold`/`Underweight`/`Sell`),
`rationale` (4-8 sentences), `strategic_actions` (concrete trader instructions).

→ `wrote reports/investment_plan.md + state/research_plan.json | RATING: <Buy|Overweight|Hold|Underweight|Sell>`

**System prompt:** Read `references/research_manager.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

**Validate** (Bash, NO LLM):

```bash
"$TA_PY" "${SKILL_DIR}/scripts/ta_parse.py" research_plan "${RUN_DIR}/state/research_plan.json"
```

If it exits non-zero (schema invalid / missing fields), **re-dispatch the research_manager
EXACTLY ONCE** with the validator's error appended to the prompt ("Your previous JSON
failed validation: <error>. Fix it."). Validate again. If it still fails, proceed but note
the defect in the Final Brief.

---

## STEP 5 — Phase 5: trader (structured) + validate

Spawn ONE trader. It Reads `reports/investment_plan.md` (the research plan) and
`reports/market_report.md` (for the latest close and key levels). It writes BOTH
`reports/trader_investment_plan.md` **and** the sidecar `state/trader_proposal.json`.

`state/trader_proposal.json` must be a single valid JSON object with EXACTLY:
`action` (one of `Buy`/`Hold`/`Sell`), `reasoning` (2-4 sentences), and optional
nullable `entry_price` (float), `stop_loss` (float), `position_sizing` (string).
Never invent a price — take entry_price from `market_report.md`.

→ `wrote reports/trader_investment_plan.md + state/trader_proposal.json | ACTION: <Buy|Hold|Sell>`

**System prompt:** Read `references/trader.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

**Validate** (Bash, NO LLM):

```bash
"$TA_PY" "${SKILL_DIR}/scripts/ta_parse.py" trader_proposal "${RUN_DIR}/state/trader_proposal.json"
```

If non-zero, **re-dispatch the trader EXACTLY ONCE** with the error appended. Validate again.

---

## STEP 6 — Phase 6: portfolio_manager (structured, FINAL) + validate

Spawn ONE portfolio_manager. It Reads `reports/investment_plan.md`,
`reports/trader_investment_plan.md`, `reports/bull.md`, `reports/bear.md`,
`reports/risk_aggressive.md`, `reports/risk_conservative.md`, `reports/risk_neutral.md`
(and may Read the analyst reports to verify a figure). **Inject `{past_memory}`** from
`context.json` so it can reference prior decisions on this ticker. It writes BOTH
`reports/final_trade_decision.md` **and** the sidecar `state/portfolio_decision.json`.

`state/portfolio_decision.json` must be a single valid JSON object with EXACTLY:
`rating` (one of `Buy`/`Overweight`/`Hold`/`Underweight`/`Sell`),
`executive_summary` (3-5 sentences), `investment_thesis` (6-12 sentences), and optional
nullable `price_target` (float), `time_horizon` (string).

→ `wrote reports/final_trade_decision.md + state/portfolio_decision.json | RATING: <Buy|Overweight|Hold|Underweight|Sell>`

**System prompt:** Read `references/portfolio_manager.md`, substitute the placeholders (per *How to dispatch* above), then send the result as this subagent's prompt.

**Validate** (Bash, NO LLM):

```bash
"$TA_PY" "${SKILL_DIR}/scripts/ta_parse.py" portfolio_decision "${RUN_DIR}/state/portfolio_decision.json"
```

If non-zero, **re-dispatch the portfolio_manager EXACTLY ONCE** with the error appended. Validate again.

---

## STEP 7 — Wrap-up (Bash, NO LLM)

**First Read `RUN_DIR/state/portfolio_decision.json`** and extract `rating`,
`price_target`, `time_horizon` — `ta_memory.py` needs them (it takes no `--run-dir`;
it requires `--rating` plus a summary source). Then assemble the combined report and
append the decision to memory, substituting the values you just read:

```bash
"$TA_PY" "${SKILL_DIR}/scripts/ta_assemble.py" --run-dir "${RUN_DIR}"

"$TA_PY" "${SKILL_DIR}/scripts/ta_memory.py" append \
  --ticker "${TICKER}" --date "${DATE}" \
  --rating "<rating from portfolio_decision.json>" \
  --summary-file "${RUN_DIR}/reports/final_trade_decision.md"
  # append --price-target <price_target> and/or --time-horizon "<time_horizon>" only if non-null
```

`ta_assemble.py` writes `reports/complete_report.md`. `ta_memory.py append` records this
decision into the per-ticker memory. Neither calls an LLM. (You already read the sidecar
here, so the Final Brief in Step 8 can reuse the same values without re-reading.)

---

## STEP 8 — Final Brief (you, the orchestrator)

Read ONLY these two small sidecars — do **not** re-read the report bodies:

- `RUN_DIR/state/portfolio_decision.json`
- `RUN_DIR/state/trader_proposal.json`

Then print a concise decision brief to the user, in Chinese, like:

```
<TICKER>（<DATE>）决策简报
- 动作 / 评级：<rating from portfolio_decision> （交易动作：<action from trader_proposal>）
- 目标价：<price_target or "未给出">
- 持有期：<time_horizon or "未给出">
- 入场：<entry_price or "未给出">
- 止损：<stop_loss or "未给出">
- 仓位：<position_sizing or "未给出">

核心论据（3-5 条）：
1. …（取自 executive_summary / investment_thesis 的要点，精炼成一句）
2. …
3. …

报告文件：
- 最终决策：<RUN_DIR>/reports/final_trade_decision.md
- 完整报告：<RUN_DIR>/reports/complete_report.md
- 各阶段报告目录：<RUN_DIR>/reports/
```

Use absolute paths for all files. Distill the 3-5 core points yourself from the
`executive_summary` / `investment_thesis` fields in the sidecar — keep each to one line.

**Only** paste the full `complete_report.md` body if the user EXPLICITLY asks for the
full report. Otherwise the brief above is the deliverable.

---

## Phase / TodoWrite checklist (one todo each)

0. Bootstrap (`ta_context.py`) → `state/context.json`
1. Phase 1 analysts (parallel) → `market_report.md`, `sentiment_report.md`, `news_report.md`, (`fundamentals_report.md` unless crypto)
2. Phase 2 bull → bear → `bull.md`, `bear.md`
3. Phase 3 risk trio (parallel) → `risk_aggressive.md`, `risk_conservative.md`, `risk_neutral.md`
4. Phase 4 research_manager (+validate) → `investment_plan.md`, `state/research_plan.json`
5. Phase 5 trader (+validate) → `trader_investment_plan.md`, `state/trader_proposal.json`
6. Phase 6 portfolio_manager (+validate) → `final_trade_decision.md`, `state/portfolio_decision.json`
7. Wrap-up (`ta_assemble.py`, `ta_memory.py`) → `complete_report.md`, memory appended
8. Final Brief (read only the 2 sidecars; print decision brief)

Remember: a phase counts as done ONLY when its `.md` file exists on disk. Reasoning lives
in subagents; scripts are pure IO. One pass, each role once, never paste report bodies
into prompts.
