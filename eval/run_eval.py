"""Eval for the billing/orders supervisor graph.

Loads shopify_merchant_tickets_test_set.csv, runs every ticket through the
graph with LangSmith tracing on, and writes eval/results.csv.

Usage (from the repo root, one command):
    .venv/bin/python eval/run_eval.py
"""

from __future__ import annotations

import csv
import os
import pathlib
import re
import sys
import time
from decimal import Decimal, InvalidOperation

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
from dotenv import load_dotenv  # noqa: E402
import supervisor as sv  # noqa: E402

CSV_PATH = ROOT / "shopify_merchant_tickets_test_set.csv"
RESULTS_PATH = ROOT / "eval" / "results.csv"
PAUSE_S = 4

# CSV tool names the mock API (and therefore the graph) calls differently.
# Resolution grading maps both sides through this table so a mock call counts
# as the expected name. expected_fix is the human description of the same action.
TOOL_ALIASES = {
    "get_billing_invoice": "list_billing_charges",
    "list_app_charges": "list_billing_charges",
    "apply_billing_credit": "issue_billing_credit",
    "get_shipping_profile": "get_delivery_profile",
    "update_order_address": "update_order_shipping_address",
}

# Tools whose order_id / amount arguments are the ticket fields named in expected_fix.
ORDER_ID_ARG_TOOLS = {
    "get_order",
    "list_order_transactions",
    "calculate_refund",
    "create_refund",
    "recalculate_order_shipping",
    "update_order_shipping_address",
    "update_order_address",
    "cancel_order",
    "add_order_note",
    "get_fulfillment",
    "update_fulfillment",
}
AMOUNT_ARG_TOOLS = {
    "calculate_refund",
    "create_refund",
    "issue_billing_credit",
    "apply_billing_credit",
    "refund_app_charge",
}

RESULT_FIELDS = [
    "ticket_id",
    "predicted_route",
    "tools_called",
    "final_status",
    "routing_pass",
    "resolution_pass",
]

_CURRENCY_RE = re.compile(r"^[A-Z]{3}\s+")
_AMOUNT_NOISE_RE = re.compile(r"[^\d.\-]")


def canonical_tool_name(name: str) -> str:
    key = (name or "").strip()
    return TOOL_ALIASES.get(key, key)


def parse_expected_tools(expected_tool_calls: str) -> list[str]:
    return [t.strip() for t in (expected_tool_calls or "").split(";") if t.strip()]


def parse_amount(value) -> Decimal | None:
    """Parse a ticket amount ('USD 29.00', 'EUR 1,062.40') or a tool arg (29 / 29.00)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    text = _CURRENCY_RE.sub("", text)
    text = text.replace(",", "")
    text = _AMOUNT_NOISE_RE.sub("", text)
    if text in {"", "-", ".", "-."}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def amounts_match(actual, expected) -> bool:
    if actual is None or actual == "" or expected is None or str(expected).strip() == "":
        return True
    left, right = parse_amount(actual), parse_amount(expected)
    if left is None or right is None:
        return str(actual).strip() == str(expected).strip()
    return left.quantize(Decimal("0.01")) == right.quantize(Decimal("0.01"))


def order_ids_match(actual, expected) -> bool:
    if actual is None or actual == "" or expected is None or str(expected).strip() == "":
        return True
    return str(actual).strip() == str(expected).strip()


def call_args_match(tool: str, args: dict, order_id: str, amount_in_question: str) -> bool:
    """True when order_id / amount arguments match the ticket fields expected_fix refers to."""
    args = args or {}
    canon = canonical_tool_name(tool)
    expected_oid = (order_id or "").strip()
    expected_amt = (amount_in_question or "").strip()
    if expected_oid and (canon in ORDER_ID_ARG_TOOLS or "order_id" in args):
        if "order_id" not in args or not order_ids_match(args.get("order_id"), expected_oid):
            return False
    if expected_amt and (canon in AMOUNT_ARG_TOOLS or "amount" in args):
        if "amount" not in args or not amounts_match(args.get("amount"), expected_amt):
            return False
    return True


def extract_tool_calls(calls: list) -> list[dict]:
    """Keep entries that named a tool. LLM/parse errors have no tool key."""
    out = []
    for call in calls or []:
        name = call.get("tool") if isinstance(call, dict) else None
        if not name:
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        out.append({"tool": name, "args": args, "status": call.get("status")})
    return out


def tools_called_cell(calls: list[dict]) -> str:
    return ";".join(c["tool"] for c in calls)


def grade_routing(predicted_route: str, expected_route: str) -> str:
    predicted = (predicted_route or "").strip()
    expected = (expected_route or "").strip()
    return "pass" if predicted and predicted == expected else "fail"


def grade_resolution(
    calls: list[dict],
    expected_tool_calls: str,
    expected_fix: str,  # noqa: ARG001 — read together with expected_tool_calls
    order_id: str,
    amount_in_question: str,
    *,
    errored: bool,
) -> str:
    """Pass when every expected tool was called with matching order_id / amount.

    Tool order does not matter. Extra calls are allowed. A run with no tool
    calls, or a run that errored, is always fail.
    """
    if errored:
        return "fail"
    if not calls:
        return "fail"
    expected_names = parse_expected_tools(expected_tool_calls)
    actual = extract_tool_calls(calls)
    if not actual:
        return "fail"
    for expected_name in expected_names:
        wanted = canonical_tool_name(expected_name)
        matching = [
            c
            for c in actual
            if canonical_tool_name(c["tool"]) == wanted
            and call_args_match(c["tool"], c["args"], order_id, amount_in_question)
        ]
        if not matching:
            return "fail"
    return "pass"


def routing_pass(run, example):
    """LangSmith evaluator: one metric, predicted_route vs expected_route."""
    run_outputs = run.outputs if hasattr(run, "outputs") else run.get("outputs", {}) or {}
    example_outputs = example.outputs if hasattr(example, "outputs") else example.get("outputs", {}) or {}
    predicted = run_outputs.get("predicted_route") or ""
    expected = example_outputs.get("expected_route") or ""
    score = 1 if grade_routing(predicted, expected) == "pass" else 0
    return {"score": score, "comment": f"predicted={predicted!r} expected={expected!r}"}


def resolution_pass(run, example):
    """LangSmith evaluator: one metric, expected tools + order_id/amount args."""
    run_outputs = run.outputs if hasattr(run, "outputs") else run.get("outputs", {}) or {}
    example_outputs = example.outputs if hasattr(example, "outputs") else example.get("outputs", {}) or {}
    status = run_outputs.get("final_status") or ""
    score = 1 if grade_resolution(
        run_outputs.get("calls") or [],
        example_outputs.get("expected_tool_calls") or "",
        example_outputs.get("expected_fix") or "",
        example_outputs.get("order_id") or "",
        example_outputs.get("amount_in_question") or "",
        errored=status.startswith("error"),
    ) == "pass" else 0
    return {"score": score, "comment": f"tools={run_outputs.get('tools_called')!r} status={status!r}"}


def run_one(app, row: dict) -> dict:
    tid = row["ticket_id"]
    shop = sv.SHOP_FOR_TICKET.get(tid, row["shop_domain"])
    sv.mock.reset_store()
    try:
        state = app.invoke(
            {"ticket": row, "shop_domain": shop, "decision": None, "error": "", "calls": []},
            config={
                "run_name": tid,
                "tags": ["eval", tid],
                "metadata": {
                    "ticket_id": tid,
                    "expected_route": row["expected_route"],
                    "expected_fix": row["expected_fix"],
                },
            },
        )
    except Exception as exc:
        return {
            "predicted_route": "",
            "tools_called": [],
            "calls": [],
            "final_status": f"error: {type(exc).__name__}: {exc}",
        }

    if state.get("decision"):
        predicted = state["decision"].get("agent") or ""
    else:
        predicted = ""
    calls = state.get("calls") or []
    tool_calls = extract_tool_calls(calls)
    if state.get("error"):
        final_status = f"error: {state['error']}"
    elif any(isinstance(c, dict) and c.get("status") == "error" for c in calls):
        messages = [
            c.get("message", "error")
            for c in calls
            if isinstance(c, dict) and c.get("status") == "error"
        ]
        final_status = f"error: {messages[0]}" if messages else "error: agent error"
    elif not tool_calls:
        final_status = "no tool calls"
    else:
        final_status = "ok"
    return {
        "predicted_route": predicted,
        "tools_called": tool_calls,
        "calls": calls,
        "final_status": final_status,
    }


def write_results(rows: list[dict]) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    load_dotenv(ROOT / ".env")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY is not set (expected in .env)")
    if not CSV_PATH.exists():
        raise SystemExit(f"missing test set: {CSV_PATH}")
    sv.enable_langsmith()

    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        tickets = list(csv.DictReader(f))
    if len(tickets) != 24:
        raise SystemExit(f"expected 24 tickets in {CSV_PATH.name}, found {len(tickets)}")

    app = sv.build_graph()
    results = []
    for i, row in enumerate(tickets):
        if i:
            time.sleep(PAUSE_S)
        tid = row["ticket_id"]
        output = run_one(app, row)
        tool_calls = output["tools_called"]
        errored = output["final_status"].startswith("error")
        routing = grade_routing(output["predicted_route"], row["expected_route"])
        resolution = grade_resolution(
            output["calls"],
            row["expected_tool_calls"],
            row["expected_fix"],
            row["order_id"],
            row["amount_in_question"],
            errored=errored,
        )
        record = {
            "ticket_id": tid,
            "predicted_route": output["predicted_route"],
            "tools_called": tools_called_cell(tool_calls),
            "final_status": output["final_status"],
            "routing_pass": routing,
            "resolution_pass": resolution,
        }
        results.append(record)
        write_results(results)
        print(
            f"{tid} predicted={record['predicted_route'] or '-'} "
            f"routing_pass={routing} resolution_pass={resolution} "
            f"status={record['final_status']}",
            flush=True,
        )

    total = len(results)
    routed = sum(r["routing_pass"] == "pass" for r in results)
    resolved = sum(r["resolution_pass"] == "pass" for r in results)
    print(f"\nrouting_pass: {routed}/{total}")
    print(f"resolution_pass: {resolved}/{total}")
    print(f"results: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
