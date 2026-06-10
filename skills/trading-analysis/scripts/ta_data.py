#!/usr/bin/env python3
"""ta_data.py — self-contained market data CLI for the trading-analysis skill.

This is the *verified data* layer of the trading-analysis skill. It is the
anti-hallucination ground truth: every exact price, volume and technical
indicator a downstream LLM agent quotes should come from here, not from the
model's imagination.

Design contract
---------------
* Depends ONLY on ``yfinance``, ``pandas``, ``stockstats`` and the Python
  stdlib. It does NOT import any project package, never mutates the import
  search path, and contains no hardcoded absolute paths. It can be copied
  anywhere and run standalone.
* Each subcommand prints MARKDOWN to stdout and is invoked via argparse with
  one subcommand per tool.
* ERROR POLICY (critical): a subcommand NEVER raises. On any failure — no
  data, network error, bad symbol, empty DataFrame — it prints a single
  sentinel line beginning ``NO_VERIFIED_DATA:`` (with the standard
  "Do NOT invent numbers..." tail) and exits 0. This keeps the calling
  agent's loop alive and explicitly tells the model not to fabricate figures.

Subcommands
-----------
* ``get_verified_snapshot <symbol> <curr_date> [--look_back_days 30]``
* ``get_stock_data <symbol> <start_date> <end_date>``
* ``get_indicators <symbol> <indicator> <curr_date> [--look_back_days 30]``
* ``get_fundamentals <ticker> <curr_date>``
* ``get_balance_sheet <ticker> [--freq quarterly] [--curr_date DATE]``
* ``get_cashflow <ticker> [--freq quarterly] [--curr_date DATE]``
* ``get_income_statement <ticker> [--freq quarterly] [--curr_date DATE]``
* ``get_news <ticker> <start_date> <end_date>``
* ``get_global_news <curr_date> [--look_back_days 7] [--limit 20]``
* ``get_insider_transactions <ticker>``

All dates are ``YYYY-MM-DD``.

__main__ usage example
----------------------
::

    # Ground-truth snapshot (prices + indicators) as of a trade date:
    python ta_data.py get_verified_snapshot AAPL 2026-06-05

    # A single indicator series over the trailing window:
    python ta_data.py get_indicators AAPL rsi 2026-06-05 --look_back_days 30

    # Fundamentals from yfinance Ticker.info:
    python ta_data.py get_fundamentals AAPL 2026-06-05

A bogus symbol (e.g. ``ZZZZNOTREAL``) prints the ``NO_VERIFIED_DATA:`` sentinel
and exits 0 rather than raising.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from datetime import datetime, timedelta

# Silence yfinance / pandas noise so stdout stays clean markdown.
warnings.filterwarnings("ignore")

try:
    import pandas as pd
    import yfinance as yf
    from stockstats import wrap as _ss_wrap
except Exception as _imp_err:  # pragma: no cover - environment guard
    sys.stdout.write(
        "NO_VERIFIED_DATA: required libraries (yfinance/pandas/stockstats) "
        f"could not be imported ({_imp_err}). Do NOT invent numbers, prices, "
        "or indicators; state that verified data could not be retrieved.\n"
    )
    raise SystemExit(0)


# --------------------------------------------------------------------------- #
# Sentinel / error policy helpers
# --------------------------------------------------------------------------- #
_SENTINEL_TAIL = (
    "Do NOT invent numbers, prices, or indicators; state that verified data "
    "could not be retrieved."
)


def _sentinel(reason: str) -> int:
    """Print the standard anti-hallucination sentinel line and return exit 0."""
    reason = (reason or "verified data unavailable").strip().rstrip(".")
    print(f"NO_VERIFIED_DATA: {reason}. {_SENTINEL_TAIL}")
    return 0


def _parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except Exception:
        return None


def _fmt_num(v, decimals: int = 2) -> str:
    """Format a possibly-missing numeric value for markdown."""
    try:
        if v is None:
            return "N/A"
        f = float(v)
        if f != f:  # NaN
            return "N/A"
        return f"{f:,.{decimals}f}"
    except Exception:
        return "N/A"


def _fmt_int(v) -> str:
    try:
        if v is None:
            return "N/A"
        f = float(v)
        if f != f:
            return "N/A"
        return f"{int(round(f)):,}"
    except Exception:
        return "N/A"


def _fmt_big(v) -> str:
    """Human-readable large number (market cap, cash, debt)."""
    try:
        if v is None:
            return "N/A"
        f = float(v)
        if f != f:
            return "N/A"
        sign = "-" if f < 0 else ""
        a = abs(f)
        for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if a >= scale:
                return f"{sign}{a / scale:,.2f}{unit}"
        return f"{sign}{a:,.2f}"
    except Exception:
        return "N/A"


# --------------------------------------------------------------------------- #
# Data download helpers
# --------------------------------------------------------------------------- #
def _flatten(df: "pd.DataFrame") -> "pd.DataFrame":
    """Normalise a yfinance OHLCV frame: flatten MultiIndex columns, lower-case,
    reset index so a 'date' column exists. Returns an empty frame on trouble."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    # The datetime column may be named 'Date', 'Datetime', or 'index'.
    rename = {}
    for c in list(df.columns):
        lc = str(c).lower()
        if lc in ("date", "datetime", "index"):
            rename[c] = "date"
        else:
            rename[c] = lc
    df = df.rename(columns=rename)
    if "date" not in df.columns:
        # Fall back to first column as date.
        df = df.rename(columns={df.columns[0]: "date"})
    # Drop duplicate columns (can happen after flattening) keeping first.
    df = df.loc[:, ~pd.Index(df.columns).duplicated()]
    try:
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    except (TypeError, AttributeError):
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    return df


def _download(symbol: str, start: str | None = None, end: str | None = None,
              period: str | None = None) -> "pd.DataFrame":
    """Download daily OHLCV via yfinance and normalise it. Empty frame on error."""
    try:
        kwargs = dict(auto_adjust=True, progress=False, interval="1d")
        if period is not None:
            df = yf.download(symbol, period=period, **kwargs)
        else:
            df = yf.download(symbol, start=start, end=end, **kwargs)
        return _flatten(df)
    except Exception:
        return pd.DataFrame()


def _history_through(symbol: str, curr_date: datetime,
                     min_rows: int = 260) -> "pd.DataFrame":
    """Daily history ending on/before ``curr_date``, with enough rows (>= min_rows
    where possible) so a 200-period SMA is valid. Rows strictly AFTER curr_date
    are excluded. Returns an empty frame on error."""
    # Pull a generous lookback (calendar days) to clear min_rows trading days.
    # ~400 trading days needs ~580 calendar days; pad further for safety.
    start = (curr_date - timedelta(days=max(750, min_rows * 2 + 120)))
    end = curr_date + timedelta(days=1)  # yfinance 'end' is exclusive
    df = _download(symbol, start=start.strftime("%Y-%m-%d"),
                   end=end.strftime("%Y-%m-%d"))
    if df.empty:
        # Retry with a period-based pull as a fallback.
        df = _download(symbol, period="3y")
    if df.empty:
        return df
    df = df[df["date"] <= curr_date].reset_index(drop=True)
    return df


def _ss(df: "pd.DataFrame"):
    """Wrap an OHLCV frame for stockstats. Expects lower-case columns + 'date'."""
    work = df.copy()
    # stockstats expects close/high/low/open/volume present.
    keep = [c for c in ("date", "open", "high", "low", "close", "volume")
            if c in work.columns]
    work = work[keep]
    return _ss_wrap(work)


# stockstats column key for each public indicator name.
_INDICATOR_KEYS = {
    "close_50_sma": "close_50_sma",
    "close_200_sma": "close_200_sma",
    "close_10_ema": "close_10_ema",
    "macd": "macd",
    "macds": "macds",
    "macdh": "macdh",
    "rsi": "rsi",          # stockstats default RSI window is 14
    "boll": "boll",
    "boll_ub": "boll_ub",
    "boll_lb": "boll_lb",
    "atr": "atr",          # stockstats default ATR window is 14
    "vwma": "vwma",
}


def _indicator_series(sdf, key: str) -> "pd.Series":
    """Return a stockstats indicator series (indexed positionally)."""
    return sdf[key]


# --------------------------------------------------------------------------- #
# Subcommand: get_verified_snapshot
# --------------------------------------------------------------------------- #
def cmd_get_verified_snapshot(symbol: str, curr_date: str,
                              look_back_days: int = 30) -> int:
    d = _parse_date(curr_date)
    if d is None:
        return _sentinel(f"invalid curr_date '{curr_date}' (expected YYYY-MM-DD)")

    df = _history_through(symbol, d, min_rows=260)
    if df.empty or "close" not in df.columns:
        return _sentinel(f"no price history for '{symbol}' on/before {curr_date}")

    # Latest verified row on/before curr_date.
    last = df.iloc[-1]
    actual_date = pd.to_datetime(last["date"]).strftime("%Y-%m-%d")

    # Compute indicators over the full history (so 200 SMA is valid).
    indicators: dict[str, str] = {}
    try:
        sdf = _ss(df)
        for name in ("close_50_sma", "close_200_sma", "close_10_ema", "rsi",
                     "macd", "macds", "macdh", "boll", "boll_ub", "boll_lb",
                     "atr"):
            try:
                series = _indicator_series(sdf, _INDICATOR_KEYS[name])
                indicators[name] = _fmt_num(series.iloc[-1], 4)
            except Exception:
                indicators[name] = "N/A"
    except Exception:
        indicators = {k: "N/A" for k in (
            "close_50_sma", "close_200_sma", "close_10_ema", "rsi", "macd",
            "macds", "macdh", "boll", "boll_ub", "boll_lb", "atr")}

    n_recent = max(15, int(look_back_days) // 2)
    recent = df.tail(n_recent)

    out: list[str] = []
    out.append(f"# Verified Snapshot — {symbol.upper()}")
    out.append("")
    out.append(f"- Requested date: **{curr_date}**")
    out.append(f"- Latest verified trading date on/before requested: "
               f"**{actual_date}**")
    out.append(f"- History rows used: **{len(df)}** "
               f"(>=250 needed for a valid 200 SMA)")
    out.append("")
    out.append("## Latest verified OHLCV")
    out.append("")
    out.append("| Field | Value |")
    out.append("| --- | --- |")
    out.append(f"| Date | {actual_date} |")
    out.append(f"| Open | {_fmt_num(last.get('open'))} |")
    out.append(f"| High | {_fmt_num(last.get('high'))} |")
    out.append(f"| Low | {_fmt_num(last.get('low'))} |")
    out.append(f"| Close | {_fmt_num(last.get('close'))} |")
    out.append(f"| Volume | {_fmt_int(last.get('volume'))} |")
    out.append("")
    out.append("## Verified technical indicators (as of latest row)")
    out.append("")
    out.append("| Indicator | Value |")
    out.append("| --- | --- |")
    out.append(f"| close_50_sma | {indicators['close_50_sma']} |")
    out.append(f"| close_200_sma | {indicators['close_200_sma']} |")
    out.append(f"| close_10_ema | {indicators['close_10_ema']} |")
    out.append(f"| rsi (14) | {indicators['rsi']} |")
    out.append(f"| macd | {indicators['macd']} |")
    out.append(f"| macds (signal) | {indicators['macds']} |")
    out.append(f"| macdh (hist) | {indicators['macdh']} |")
    out.append(f"| boll (mid) | {indicators['boll']} |")
    out.append(f"| boll_ub | {indicators['boll_ub']} |")
    out.append(f"| boll_lb | {indicators['boll_lb']} |")
    out.append(f"| atr (14) | {indicators['atr']} |")
    out.append("")
    out.append(f"## Recent verified closes (last ~{len(recent)} rows)")
    out.append("")
    for _, row in recent.iterrows():
        ds = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
        out.append(f"- {ds}: {_fmt_num(row.get('close'))}")
    out.append("")
    out.append("---")
    out.append("This snapshot is the source of truth for exact numbers. "
               "Do NOT invent prices or indicator values.")
    print("\n".join(out))
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: get_stock_data
# --------------------------------------------------------------------------- #
def cmd_get_stock_data(symbol: str, start_date: str, end_date: str) -> int:
    sd, ed = _parse_date(start_date), _parse_date(end_date)
    if sd is None or ed is None:
        return _sentinel(f"invalid date range '{start_date}'..'{end_date}' "
                         "(expected YYYY-MM-DD)")
    if sd > ed:
        return _sentinel(f"start_date {start_date} is after end_date {end_date}")

    # yfinance 'end' is exclusive; add a day to make the range inclusive.
    end_excl = (ed + timedelta(days=1)).strftime("%Y-%m-%d")
    df = _download(symbol, start=start_date, end=end_excl)
    if df.empty or "close" not in df.columns:
        return _sentinel(f"no price data for '{symbol}' in "
                         f"{start_date}..{end_date}")
    df = df[(df["date"] >= sd) & (df["date"] <= ed)].reset_index(drop=True)
    if df.empty:
        return _sentinel(f"no trading rows for '{symbol}' in "
                         f"{start_date}..{end_date}")

    out: list[str] = []
    out.append(f"# OHLCV — {symbol.upper()} ({start_date} to {end_date})")
    out.append("")
    out.append(f"Rows: **{len(df)}**")
    out.append("")
    out.append("| Date | Open | High | Low | Close | Volume |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for _, row in df.iterrows():
        ds = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
        out.append(
            f"| {ds} | {_fmt_num(row.get('open'))} | {_fmt_num(row.get('high'))} "
            f"| {_fmt_num(row.get('low'))} | {_fmt_num(row.get('close'))} "
            f"| {_fmt_int(row.get('volume'))} |"
        )
    print("\n".join(out))
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: get_indicators
# --------------------------------------------------------------------------- #
def cmd_get_indicators(symbol: str, indicator: str, curr_date: str,
                       look_back_days: int = 30) -> int:
    d = _parse_date(curr_date)
    if d is None:
        return _sentinel(f"invalid curr_date '{curr_date}' (expected YYYY-MM-DD)")
    ind = indicator.strip().lower()
    if ind not in _INDICATOR_KEYS:
        return _sentinel(
            f"unknown indicator '{indicator}'. Supported: "
            + ", ".join(sorted(_INDICATOR_KEYS))
        )

    df = _history_through(symbol, d, min_rows=260)
    if df.empty or "close" not in df.columns:
        return _sentinel(f"no price history for '{symbol}' on/before {curr_date}")

    try:
        sdf = _ss(df)
        series = _indicator_series(sdf, _INDICATOR_KEYS[ind])
    except Exception as e:
        return _sentinel(f"could not compute '{ind}' for '{symbol}' ({e})")

    # Align the computed series with the date column by position.
    values = pd.Series(list(series), index=range(len(series)))
    dates = df["date"].reset_index(drop=True)
    n = min(len(values), len(dates))
    window = max(1, int(look_back_days))
    start_idx = max(0, n - window)

    rows: list[tuple[str, str]] = []
    for i in range(start_idx, n):
        ds = pd.to_datetime(dates.iloc[i]).strftime("%Y-%m-%d")
        rows.append((ds, _fmt_num(values.iloc[i], 4)))
    if not rows:
        return _sentinel(f"no '{ind}' values available for '{symbol}'")

    out: list[str] = []
    out.append(f"# Indicator {ind} — {symbol.upper()}")
    out.append("")
    out.append(f"- As of: **{curr_date}** (latest verified row: "
               f"**{rows[-1][0]}**)")
    out.append(f"- Look-back window: **{window}** trading days "
               f"(showing {len(rows)} values)")
    out.append("")
    for ds, val in rows:
        out.append(f"- {ds}: {val}")
    out.append("")
    out.append("---")
    out.append("Indicator values are computed from verified history. "
               "Do NOT invent values.")
    print("\n".join(out))
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: get_fundamentals
# --------------------------------------------------------------------------- #
def cmd_get_fundamentals(ticker: str, curr_date: str) -> int:
    # curr_date is accepted for signature consistency / provenance only.
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        info = {}
    if not info or len([k for k in info if info.get(k) is not None]) < 3:
        return _sentinel(f"no fundamentals available for '{ticker}'")

    def g(*keys):
        for k in keys:
            v = info.get(k)
            if v is not None:
                return v
        return None

    name = g("longName", "shortName") or ticker.upper()

    rows: list[tuple[str, str]] = [
        ("Trailing P/E", _fmt_num(g("trailingPE"))),
        ("Forward P/E", _fmt_num(g("forwardPE"))),
        ("PEG ratio", _fmt_num(g("pegRatio", "trailingPegRatio"))),
        ("Price/Book", _fmt_num(g("priceToBook"))),
        ("Profit margin", _pct(g("profitMargins"))),
        ("Operating margin", _pct(g("operatingMargins"))),
        ("Gross margin", _pct(g("grossMargins"))),
        ("Return on equity (ROE)", _pct(g("returnOnEquity"))),
        ("Revenue growth (yoy)", _pct(g("revenueGrowth"))),
        ("Earnings growth (yoy)", _pct(g("earningsGrowth"))),
        ("Beta", _fmt_num(g("beta"))),
        ("Short ratio", _fmt_num(g("shortRatio"))),
        ("Market cap", _fmt_big(g("marketCap"))),
        ("Total cash", _fmt_big(g("totalCash"))),
        ("Total debt", _fmt_big(g("totalDebt"))),
        ("Debt/Equity", _fmt_num(g("debtToEquity"))),
        ("Current ratio", _fmt_num(g("currentRatio"))),
        ("Analyst recommendation (mean)",
         f"{_fmt_num(g('recommendationMean'))}"
         + (f" ({g('recommendationKey')})" if g("recommendationKey") else "")),
        ("Analyst target (mean)", _fmt_num(g("targetMeanPrice"))),
    ]

    out: list[str] = []
    out.append(f"# Fundamentals — {name} ({ticker.upper()})")
    out.append("")
    out.append(f"- As of: **{curr_date}** (yfinance Ticker.info snapshot)")
    cur_price = g("currentPrice", "regularMarketPrice")
    if cur_price is not None:
        out.append(f"- Current price (info): **{_fmt_num(cur_price)}**")
    out.append("")
    out.append("| Metric | Value |")
    out.append("| --- | --- |")
    for label, val in rows:
        out.append(f"| {label} | {val} |")
    out.append("")
    out.append("---")
    out.append("Fundamentals are sourced from yfinance and may lag. "
               "Do NOT invent figures.")
    print("\n".join(out))
    return 0


def _pct(v) -> str:
    try:
        if v is None:
            return "N/A"
        f = float(v)
        if f != f:
            return "N/A"
        return f"{f * 100:,.2f}%"
    except Exception:
        return "N/A"


# --------------------------------------------------------------------------- #
# Subcommands: financial statements
# --------------------------------------------------------------------------- #
# Key line items to surface per statement (label, list of acceptable row names).
_BALANCE_ITEMS = [
    ("Total Assets", ["Total Assets"]),
    ("Total Liabilities", ["Total Liabilities Net Minority Interest",
                           "Total Liabilities"]),
    ("Total Equity", ["Total Equity Gross Minority Interest", "Stockholders Equity",
                      "Total Stockholder Equity"]),
    ("Cash & Equivalents", ["Cash And Cash Equivalents",
                            "Cash Cash Equivalents And Short Term Investments"]),
    ("Total Debt", ["Total Debt"]),
    ("Current Assets", ["Current Assets", "Total Current Assets"]),
    ("Current Liabilities", ["Current Liabilities", "Total Current Liabilities"]),
    ("Working Capital", ["Working Capital"]),
    ("Retained Earnings", ["Retained Earnings"]),
    ("Shares Outstanding", ["Ordinary Shares Number", "Share Issued"]),
]

_CASHFLOW_ITEMS = [
    ("Operating Cash Flow", ["Operating Cash Flow",
                             "Total Cash From Operating Activities"]),
    ("Investing Cash Flow", ["Investing Cash Flow",
                             "Total Cashflows From Investing Activities"]),
    ("Financing Cash Flow", ["Financing Cash Flow",
                             "Total Cash From Financing Activities"]),
    ("Capital Expenditure", ["Capital Expenditure", "Capital Expenditures"]),
    ("Free Cash Flow", ["Free Cash Flow"]),
    ("Net Income", ["Net Income", "Net Income From Continuing Operations"]),
    ("Repurchase of Stock", ["Repurchase Of Capital Stock"]),
    ("Cash Dividends Paid", ["Cash Dividends Paid", "Common Stock Dividend Paid"]),
    ("Change in Cash", ["Changes In Cash", "Change In Cash"]),
]

_INCOME_ITEMS = [
    ("Total Revenue", ["Total Revenue", "Operating Revenue"]),
    ("Cost of Revenue", ["Cost Of Revenue"]),
    ("Gross Profit", ["Gross Profit"]),
    ("Operating Income", ["Operating Income", "Operating Income Loss"]),
    ("EBITDA", ["EBITDA", "Normalized EBITDA"]),
    ("EBIT", ["EBIT"]),
    ("Pretax Income", ["Pretax Income"]),
    ("Net Income", ["Net Income", "Net Income Common Stockholders"]),
    ("Basic EPS", ["Basic EPS"]),
    ("Diluted EPS", ["Diluted EPS"]),
]


def _statement_df(ticker: str, kind: str, freq: str):
    """Fetch a financial statement DataFrame from yfinance. None on failure."""
    quarterly = not str(freq).lower().startswith("a")
    try:
        t = yf.Ticker(ticker)
        if kind == "balance":
            df = t.quarterly_balance_sheet if quarterly else t.balance_sheet
        elif kind == "cashflow":
            df = t.quarterly_cashflow if quarterly else t.cashflow
        else:  # income
            df = (t.quarterly_income_stmt if quarterly else t.income_stmt)
        if df is None or getattr(df, "empty", True):
            return None
        return df
    except Exception:
        return None


def _render_statement(df, items, title, ticker, freq, curr_date) -> str:
    quarterly = not str(freq).lower().startswith("a")
    freq_label = "quarterly" if quarterly else "annual"

    # Columns are period-end timestamps; keep most recent 4, newest first.
    cols = list(df.columns)
    # Optionally bound by curr_date if provided.
    if curr_date:
        cd = _parse_date(curr_date)
        if cd is not None:
            kept = []
            for c in cols:
                try:
                    if pd.to_datetime(c).tz_localize(None) <= cd:
                        kept.append(c)
                except Exception:
                    kept.append(c)
            if kept:
                cols = kept
    cols = cols[:4]
    period_labels = [pd.to_datetime(c).strftime("%Y-%m-%d")
                     if not isinstance(c, str) else str(c) for c in cols]

    out: list[str] = []
    out.append(f"# {title} — {ticker.upper()} ({freq_label})")
    out.append("")
    out.append(f"Most recent {len(cols)} periods"
               + (f" (<= {curr_date})" if curr_date else "") + ".")
    out.append("")
    header = "| Line item | " + " | ".join(period_labels) + " |"
    sep = "| --- | " + " | ".join(["---"] * len(cols)) + " |"
    out.append(header)
    out.append(sep)

    index_set = list(df.index)
    for label, candidates in items:
        row_name = next((c for c in candidates if c in index_set), None)
        cells = []
        for c in cols:
            if row_name is None:
                cells.append("N/A")
            else:
                try:
                    cells.append(_fmt_big(df.loc[row_name, c]))
                except Exception:
                    cells.append("N/A")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("---")
    out.append("Figures are from yfinance financial statements. "
               "Do NOT invent figures.")
    return "\n".join(out)


def cmd_get_balance_sheet(ticker: str, freq: str = "quarterly",
                          curr_date: str | None = None) -> int:
    df = _statement_df(ticker, "balance", freq)
    if df is None:
        return _sentinel(f"no balance sheet available for '{ticker}'")
    print(_render_statement(df, _BALANCE_ITEMS, "Balance Sheet",
                            ticker, freq, curr_date))
    return 0


def cmd_get_cashflow(ticker: str, freq: str = "quarterly",
                     curr_date: str | None = None) -> int:
    df = _statement_df(ticker, "cashflow", freq)
    if df is None:
        return _sentinel(f"no cash flow statement available for '{ticker}'")
    print(_render_statement(df, _CASHFLOW_ITEMS, "Cash Flow Statement",
                            ticker, freq, curr_date))
    return 0


def cmd_get_income_statement(ticker: str, freq: str = "quarterly",
                             curr_date: str | None = None) -> int:
    df = _statement_df(ticker, "income", freq)
    if df is None:
        return _sentinel(f"no income statement available for '{ticker}'")
    print(_render_statement(df, _INCOME_ITEMS, "Income Statement",
                            ticker, freq, curr_date))
    return 0


# --------------------------------------------------------------------------- #
# News helpers
# --------------------------------------------------------------------------- #
def _news_items(symbol: str) -> list[dict]:
    """Return a list of normalised news dicts: title, publisher, ts (datetime|None),
    link. Robust to both legacy-flat and newer nested yfinance schemas."""
    try:
        raw = yf.Ticker(symbol).news or []
    except Exception:
        return []
    items: list[dict] = []
    for entry in raw:
        try:
            content = entry.get("content") if isinstance(entry, dict) else None
            c = content if isinstance(content, dict) else entry
            title = c.get("title") or entry.get("title")
            if not title:
                continue
            # Publisher / provider.
            publisher = None
            prov = c.get("provider")
            if isinstance(prov, dict):
                publisher = prov.get("displayName")
            publisher = publisher or entry.get("publisher") or "Unknown"
            # Timestamp: nested ISO 'pubDate' or legacy epoch 'providerPublishTime'.
            ts = None
            pub = c.get("pubDate") or c.get("displayTime")
            if pub:
                try:
                    ts = pd.to_datetime(pub).tz_localize(None)
                except Exception:
                    ts = None
            if ts is None:
                epoch = entry.get("providerPublishTime") or c.get(
                    "providerPublishTime")
                if epoch:
                    try:
                        ts = datetime.utcfromtimestamp(float(epoch))
                    except Exception:
                        ts = None
            # Link.
            link = None
            for k in ("canonicalUrl", "clickThroughUrl"):
                u = c.get(k)
                if isinstance(u, dict) and u.get("url"):
                    link = u["url"]
                    break
            link = link or entry.get("link")
            items.append({"title": title, "publisher": publisher,
                          "ts": ts, "link": link})
        except Exception:
            continue
    return items


def cmd_get_news(ticker: str, start_date: str, end_date: str) -> int:
    sd, ed = _parse_date(start_date), _parse_date(end_date)
    if sd is None or ed is None:
        return _sentinel(f"invalid date range '{start_date}'..'{end_date}' "
                         "(expected YYYY-MM-DD)")
    items = _news_items(ticker)
    if not items:
        return _sentinel(f"no news available for '{ticker}'")

    ed_incl = ed + timedelta(days=1)
    dated = [it for it in items if it["ts"] is not None]
    undated_present = any(it["ts"] is None for it in items)
    window_filtered = True
    if dated:
        in_window = [it for it in dated
                     if sd <= it["ts"] < ed_incl]
        if in_window:
            chosen = sorted(in_window, key=lambda x: x["ts"], reverse=True)
        else:
            # No items in window; fall back to most recent, note it.
            window_filtered = False
            chosen = sorted(dated, key=lambda x: x["ts"], reverse=True)
    else:
        window_filtered = False
        chosen = items
    chosen = chosen[:15]

    dated_chosen = [it for it in chosen if it["ts"] is not None]
    _day = lambda t: t.date() if hasattr(t, "date") else t
    span_lo = min((it["ts"] for it in dated_chosen), default=None)
    span_hi = max((it["ts"] for it in dated_chosen), default=None)
    distinct_days = len({_day(it["ts"]) for it in dated_chosen})
    req_days = max(1, (ed - sd).days)

    out: list[str] = []
    out.append(f"# News — {ticker.upper()}")
    out.append("")
    out.append(f"- Requested window: {start_date} to {end_date}")
    if span_lo is not None:
        out.append(f"- Actually covered: {span_lo.strftime('%Y-%m-%d')} to "
                   f"{span_hi.strftime('%Y-%m-%d')} "
                   f"(**{len(chosen)}** headlines across {distinct_days} day(s))")
    else:
        out.append(f"- **{len(chosen)}** headlines (timestamps unavailable)")
    out.append("")
    # yfinance exposes only the latest ~10 articles, with no historical range
    # query — so a multi-day window is usually only filled on its most recent
    # day(s). Warn explicitly so the reader does not over-weight a thin sample.
    thin = (span_lo is None or distinct_days <= 2
            or (span_hi - span_lo).days < req_days // 2)
    if thin:
        out.append("> ⚠ yfinance provides only the latest ~10 headlines and has no "
                   "historical range query, so older items in the requested window "
                   "are unavailable. Treat this as a CURRENT news snapshot, not a full "
                   f"{req_days}-day sample, and weight confidence accordingly.")
        out.append("")
    elif undated_present:
        out.append("> Note: some headlines lacked timestamps and were excluded "
                   "from the window filter.")
        out.append("")
    for it in chosen:
        ds = it["ts"].strftime("%Y-%m-%d") if it["ts"] is not None else "undated"
        line = f"- **{ds}** — {it['title']} _({it['publisher']})_"
        if it.get("link"):
            line += f"  \n  {it['link']}"
        out.append(line)
    print("\n".join(out))
    return 0


def cmd_get_global_news(curr_date: str, look_back_days: int = 7,
                        limit: int = 20) -> int:
    cd = _parse_date(curr_date)
    if cd is None:
        return _sentinel(f"invalid curr_date '{curr_date}' (expected YYYY-MM-DD)")
    window_start = cd - timedelta(days=max(1, int(look_back_days)))
    cd_incl = cd + timedelta(days=1)

    proxies = [
        ("^GSPC", "S&P 500"),
        ("^TNX", "10Y Treasury Yield"),
        ("CL=F", "Crude Oil (WTI)"),
        ("GC=F", "Gold"),
    ]
    collected: list[dict] = []
    seen_titles: set[str] = set()
    used_proxies: list[str] = []
    for sym, label in proxies:
        try:
            items = _news_items(sym)
        except Exception:
            continue
        if not items:
            continue
        used_proxies.append(label)
        for it in items:
            title = it["title"]
            if title in seen_titles:
                continue
            ts = it["ts"]
            # Keep items in window, or undated items as a fallback context.
            if ts is not None and not (window_start <= ts < cd_incl):
                continue
            seen_titles.add(title)
            it = dict(it)
            it["proxy"] = label
            collected.append(it)

    if not collected:
        return _sentinel(
            f"no macro/global news retrievable for window ending {curr_date}")

    # Sort newest first; undated sink to the bottom.
    collected.sort(
        key=lambda x: x["ts"] if x["ts"] is not None else datetime.min,
        reverse=True,
    )
    collected = collected[:max(1, int(limit))]

    out: list[str] = []
    out.append(f"# Global / Macro News — window ending {curr_date}")
    out.append("")
    _gday = lambda t: t.date() if hasattr(t, "date") else t
    g_dated = [it["ts"] for it in collected if it["ts"] is not None]
    g_days = len({_gday(t) for t in g_dated})
    g_lo = min(g_dated, default=None)
    g_hi = max(g_dated, default=None)

    out.append(f"- Requested look-back: **{look_back_days}** days "
               f"({window_start.strftime('%Y-%m-%d')} to {curr_date})")
    if g_lo is not None:
        out.append(f"- Actually covered: {g_lo.strftime('%Y-%m-%d')} to "
                   f"{g_hi.strftime('%Y-%m-%d')} across {g_days} day(s)")
    out.append(f"- Proxies used: {', '.join(used_proxies) or 'none'}")
    out.append(f"- Showing **{len(collected)}** of up to {limit} headlines.")
    out.append("")
    if g_lo is None or g_days <= 2:
        out.append("> ⚠ yfinance macro headlines are latest-only (no historical "
                   "range query), so most cluster on the final day(s). Treat as a "
                   "current macro snapshot, not an evenly-sampled look-back window.")
        out.append("")
    for it in collected:
        ds = it["ts"].strftime("%Y-%m-%d") if it["ts"] is not None else "undated"
        line = (f"- **{ds}** [{it['proxy']}] — {it['title']} "
                f"_({it['publisher']})_")
        if it.get("link"):
            line += f"  \n  {it['link']}"
        out.append(line)
    print("\n".join(out))
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: get_insider_transactions
# --------------------------------------------------------------------------- #
def cmd_get_insider_transactions(ticker: str) -> int:
    try:
        df = yf.Ticker(ticker).insider_transactions
    except Exception:
        df = None
    if df is None or getattr(df, "empty", True):
        return _sentinel(f"no insider transactions available for '{ticker}'")

    df = df.copy()
    # Sort newest first by 'Start Date' when present.
    if "Start Date" in df.columns:
        try:
            df["_d"] = pd.to_datetime(df["Start Date"], errors="coerce")
            df = df.sort_values("_d", ascending=False).drop(columns="_d")
        except Exception:
            pass
    df = df.head(20)

    def col(row, *names):
        for n in names:
            if n in row and pd.notna(row[n]):
                v = row[n]
                # Skip empty/whitespace strings so we fall through to a
                # more informative column (e.g. yfinance leaves the
                # 'Transaction' code blank but fills 'Text').
                if isinstance(v, str) and not v.strip():
                    continue
                return v
        return None

    out: list[str] = []
    out.append(f"# Insider Transactions — {ticker.upper()}")
    out.append("")
    out.append(f"Showing **{len(df)}** most recent transactions.")
    out.append("")
    out.append("| Date | Insider | Position | Transaction | Shares | Value |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for _, row in df.iterrows():
        date_v = col(row, "Start Date")
        try:
            date_s = (pd.to_datetime(date_v).strftime("%Y-%m-%d")
                      if date_v is not None else "N/A")
        except Exception:
            date_s = str(date_v) if date_v is not None else "N/A"
        insider = col(row, "Insider") or "N/A"
        position = col(row, "Position") or "N/A"
        txn = col(row, "Text", "Transaction") or "N/A"
        shares = _fmt_int(col(row, "Shares"))
        value = _fmt_big(col(row, "Value"))
        out.append(f"| {date_s} | {insider} | {position} | {txn} | "
                   f"{shares} | {value} |")
    out.append("")
    out.append("---")
    out.append("Insider data is from yfinance and may be incomplete. "
               "Do NOT invent transactions.")
    print("\n".join(out))
    return 0


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ta_data.py",
        description="Self-contained verified market-data CLI (yfinance + "
                    "stockstats). Anti-hallucination ground truth for the "
                    "trading-analysis skill.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("get_verified_snapshot",
                       help="Verified OHLCV + indicators ground truth.")
    s.add_argument("symbol")
    s.add_argument("curr_date")
    s.add_argument("--look_back_days", type=int, default=30)

    s = sub.add_parser("get_stock_data", help="OHLCV table for a date range.")
    s.add_argument("symbol")
    s.add_argument("start_date")
    s.add_argument("end_date")

    s = sub.add_parser("get_indicators",
                       help="One stockstats indicator series over a window.")
    s.add_argument("symbol")
    s.add_argument("indicator")
    s.add_argument("curr_date")
    s.add_argument("--look_back_days", type=int, default=30)

    s = sub.add_parser("get_fundamentals",
                       help="Key fundamentals from yfinance Ticker.info.")
    s.add_argument("ticker")
    s.add_argument("curr_date")

    s = sub.add_parser("get_balance_sheet", help="Quarterly/annual balance sheet.")
    s.add_argument("ticker")
    s.add_argument("--freq", default="quarterly")
    s.add_argument("--curr_date", default=None)

    s = sub.add_parser("get_cashflow", help="Quarterly/annual cash flow.")
    s.add_argument("ticker")
    s.add_argument("--freq", default="quarterly")
    s.add_argument("--curr_date", default=None)

    s = sub.add_parser("get_income_statement",
                       help="Quarterly/annual income statement.")
    s.add_argument("ticker")
    s.add_argument("--freq", default="quarterly")
    s.add_argument("--curr_date", default=None)

    s = sub.add_parser("get_news", help="Ticker headlines in a date range.")
    s.add_argument("ticker")
    s.add_argument("start_date")
    s.add_argument("end_date")

    s = sub.add_parser("get_global_news", help="Macro/global news via proxies.")
    s.add_argument("curr_date")
    s.add_argument("--look_back_days", type=int, default=7)
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("get_insider_transactions",
                       help="Recent insider transactions.")
    s.add_argument("ticker")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.command

    # Dispatch with a blanket guard so nothing ever raises out of a subcommand.
    try:
        if cmd == "get_verified_snapshot":
            return cmd_get_verified_snapshot(
                args.symbol, args.curr_date, args.look_back_days)
        if cmd == "get_stock_data":
            return cmd_get_stock_data(args.symbol, args.start_date, args.end_date)
        if cmd == "get_indicators":
            return cmd_get_indicators(
                args.symbol, args.indicator, args.curr_date, args.look_back_days)
        if cmd == "get_fundamentals":
            return cmd_get_fundamentals(args.ticker, args.curr_date)
        if cmd == "get_balance_sheet":
            return cmd_get_balance_sheet(args.ticker, args.freq, args.curr_date)
        if cmd == "get_cashflow":
            return cmd_get_cashflow(args.ticker, args.freq, args.curr_date)
        if cmd == "get_income_statement":
            return cmd_get_income_statement(args.ticker, args.freq, args.curr_date)
        if cmd == "get_news":
            return cmd_get_news(args.ticker, args.start_date, args.end_date)
        if cmd == "get_global_news":
            return cmd_get_global_news(
                args.curr_date, args.look_back_days, args.limit)
        if cmd == "get_insider_transactions":
            return cmd_get_insider_transactions(args.ticker)
        return _sentinel(f"unknown command '{cmd}'")
    except Exception as e:  # absolute backstop — never raise out of a subcommand
        return _sentinel(f"unexpected error in {cmd} ({type(e).__name__}: {e})")


if __name__ == "__main__":
    raise SystemExit(main())
