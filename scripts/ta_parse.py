#!/usr/bin/env python3
"""ta_parse.py — non-LLM validator for the three structured sidecar JSON files.

This is the pure-Python fallback that replaces the SDK's structured-output
guarantee. The three decision-making roles (Research Manager, Trader,
Portfolio Manager) each write a sidecar JSON next to their markdown report;
the orchestrator runs this script to confirm the sidecar conforms to its
contract before handing it downstream. On failure the orchestrator
re-dispatches that role once.

Contract source of truth: the JSON Schemas embedded below were ported from the
Pydantic models in
    the TradingAgents schemas (ResearchPlan / TraderProposal / PortfolioDecision)
(ResearchPlan, TraderProposal, PortfolioDecision).

Constraints (see SKILL FIXED PROTOCOL): pure Python, stdlib only
(json + argparse). Uses the `jsonschema` package if it happens to be importable,
otherwise falls back to a minimal hand-rolled validator covering the schema
features actually used here (object/required/additionalProperties/enum/type,
including nullable union types). NO claude_agent_sdk, NO LLM, NO network.

CLI:
    python ta_parse.py <research_plan|trader_proposal|portfolio_decision> <path-to-json>

Exit 0 and print "OK" when the file parses and validates.
Exit 1 and print human-readable error lines otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys


# ---------------------------------------------------------------------------
# Embedded JSON Schemas (ported from agents/schemas.py — draft-07)
# ---------------------------------------------------------------------------

RESEARCH_PLAN_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ResearchPlan",
    "description": (
        "Structured investment plan produced by the Research Manager. Hand-off "
        "to the Trader: the recommendation pins the directional view, the "
        "rationale captures which side of the bull/bear debate carried the "
        "argument, and the strategic actions translate that into concrete "
        "instructions the trader can execute against."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["recommendation", "rationale", "strategic_actions"],
    "properties": {
        "recommendation": {
            "type": "string",
            "enum": ["Buy", "Overweight", "Hold", "Underweight", "Sell"],
            "description": (
                "The investment recommendation. Exactly one of Buy / Overweight "
                "/ Hold / Underweight / Sell. Reserve Hold for situations where "
                "the evidence on both sides is genuinely balanced; otherwise "
                "commit to the side with the stronger arguments."
            ),
        },
        "rationale": {
            "type": "string",
            "description": (
                "Conversational summary of the key points from both sides of "
                "the debate, ending with which arguments led to the "
                "recommendation. Speak naturally, as if to a teammate."
            ),
        },
        "strategic_actions": {
            "type": "string",
            "description": (
                "Concrete steps for the trader to implement the recommendation, "
                "including position sizing guidance consistent with the rating."
            ),
        },
    },
}

TRADER_PROPOSAL_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "TraderProposal",
    "description": (
        "Structured transaction proposal produced by the Trader. The trader "
        "reads the Research Manager's investment plan and the analyst reports, "
        "then turns them into a concrete transaction: what action to take, the "
        "reasoning that justifies it, and the practical levels for entry, "
        "stop-loss, and sizing."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "reasoning"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["Buy", "Hold", "Sell"],
            "description": (
                "The transaction direction. Exactly one of Buy / Hold / Sell."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": (
                "The case for this action, anchored in the analysts' reports "
                "and the research plan. Two to four sentences."
            ),
        },
        "entry_price": {
            "type": ["number", "null"],
            "default": None,
            "description": (
                "Optional entry price target in the instrument's quote currency."
            ),
        },
        "stop_loss": {
            "type": ["number", "null"],
            "default": None,
            "description": (
                "Optional stop-loss price in the instrument's quote currency."
            ),
        },
        "position_sizing": {
            "type": ["string", "null"],
            "default": None,
            "description": "Optional sizing guidance, e.g. '5% of portfolio'.",
        },
    },
}

PORTFOLIO_DECISION_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "PortfolioDecision",
    "description": (
        "Structured output produced by the Portfolio Manager. The model fills "
        "every field as part of its primary LLM call; no separate extraction "
        "pass is required."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["rating", "executive_summary", "investment_thesis"],
    "properties": {
        "rating": {
            "type": "string",
            "enum": ["Buy", "Overweight", "Hold", "Underweight", "Sell"],
            "description": (
                "The final position rating. Exactly one of Buy / Overweight / "
                "Hold / Underweight / Sell, picked based on the analysts' debate."
            ),
        },
        "executive_summary": {
            "type": "string",
            "description": (
                "A concise action plan covering entry strategy, position "
                "sizing, key risk levels, and time horizon. Two to four "
                "sentences."
            ),
        },
        "investment_thesis": {
            "type": "string",
            "description": (
                "Detailed reasoning anchored in specific evidence from the "
                "analysts' debate. If prior lessons are referenced in the "
                "prompt context, incorporate them; otherwise rely solely on the "
                "current analysis."
            ),
        },
        "price_target": {
            "type": ["number", "null"],
            "default": None,
            "description": (
                "Optional target price in the instrument's quote currency."
            ),
        },
        "time_horizon": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "Optional recommended holding period, e.g. '3-6 months'."
            ),
        },
    },
}

SCHEMAS = {
    "research_plan": RESEARCH_PLAN_SCHEMA,
    "trader_proposal": TRADER_PROPOSAL_SCHEMA,
    "portfolio_decision": PORTFOLIO_DECISION_SCHEMA,
}


# ---------------------------------------------------------------------------
# Minimal hand-rolled validator (used when `jsonschema` is unavailable)
# ---------------------------------------------------------------------------

# JSON Schema type name -> Python type(s). bool is excluded from "number"/
# "integer" because in JSON Schema booleans are NOT numbers (and Python's
# bool is a subclass of int, which would otherwise let True pass as a number).
def _matches_type(value, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    # Unknown type keyword: do not block.
    return True


def _validate_minimal(instance, schema, path="$"):
    """Validate `instance` against `schema`. Return a list of error strings.

    Supports only the JSON Schema features used by the three embedded schemas:
    type (string or list-of-strings), enum, required, properties,
    additionalProperties:false, and object. This is intentionally small — it is
    a fallback, not a general-purpose validator.
    """
    errors = []

    # --- type ---
    type_kw = schema.get("type")
    if type_kw is not None:
        allowed = type_kw if isinstance(type_kw, list) else [type_kw]
        if not any(_matches_type(instance, t) for t in allowed):
            got = "null" if instance is None else type(instance).__name__
            errors.append(
                f"{path}: expected type {' or '.join(allowed)}, got {got}"
            )
            # If the type is wrong, deeper checks would be noise.
            return errors

    # --- enum ---
    enum_kw = schema.get("enum")
    if enum_kw is not None and instance not in enum_kw:
        errors.append(
            f"{path}: value {instance!r} is not one of the allowed values "
            f"{enum_kw}"
        )

    # --- object: required / properties / additionalProperties ---
    if isinstance(instance, dict):
        props = schema.get("properties", {})

        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: missing required field '{req}'")

        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    errors.append(f"{path}: unexpected field '{key}' "
                                  f"(additionalProperties not allowed)")

        for key, subschema in props.items():
            if key in instance:
                errors.extend(
                    _validate_minimal(instance[key], subschema, f"{path}.{key}")
                )

    return errors


def _validate(instance, schema):
    """Validate using `jsonschema` if importable, else the minimal validator.

    Returns a list of human-readable error strings (empty list == valid).
    """
    try:
        import jsonschema  # type: ignore
        from jsonschema import Draft7Validator
    except Exception:
        return _validate_minimal(instance, schema)

    validator = Draft7Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        loc = "$" + "".join(f".{p}" for p in err.path)
        errors.append(f"{loc}: {err.message}")
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ta_parse.py",
        description=(
            "Validate a structured sidecar JSON file against its contract "
            "(research_plan / trader_proposal / portfolio_decision)."
        ),
    )
    parser.add_argument(
        "kind",
        choices=sorted(SCHEMAS.keys()),
        help="Which sidecar schema to validate against.",
    )
    parser.add_argument(
        "path",
        help="Path to the JSON file to validate.",
    )
    args = parser.parse_args(argv)

    schema = SCHEMAS[args.kind]

    # --- read ---
    try:
        with open(args.path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        print(f"ERROR: file not found: {args.path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: could not read {args.path}: {exc}", file=sys.stderr)
        return 1

    # --- parse ---
    try:
        instance = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: {args.path} is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return 1

    # --- validate ---
    errors = _validate(instance, schema)
    if errors:
        print(
            f"ERROR: {args.path} failed {args.kind} validation "
            f"({len(errors)} problem(s)):",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
