"""Eval for the supervisor graph: run every ticket, save a per-node trace, score routing and resolution.

This script does not change agents/supervisor.py. It wraps the module's functions (triage,
run_agent, chat, execute) before the graph is built, so every model reply and every call
is recorded under the node that made it.

Routing: predicted agent == CSV expected_route.
Resolution: the successful write calls match the CSV's expected writes. It checks which
functions were written, not the values written. Escalation tickets pass only with no
successful writes. A ticket is not_evaluable when its shop is not in the fake store, or
when the CSV names a write with no matching mock function.

Usage (from the repo root):
    .venv/bin/python eval/run_eval.py

Writes eval/traces/<ticket_id>.json and eval/results.csv.
"""

import csv
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
from dotenv import load_dotenv  # noqa: E402
import supervisor as sv  # noqa: E402

TRACE_DIR = ROOT / "eval" / "traces"
RESULTS_PATH = ROOT / "eval" / "results.csv"
PAUSE_S = 4

# CSV tool names that the mock API calls differently.
ALIAS = {
    "get_billing_invoice": "list_billing_charges",
    "list_app_charges": "list_billing_charges",
    "apply_billing_credit": "issue_billing_credit",
    "get_shipping_profile": "get_delivery_profile",
    "update_order_address": "update_order_shipping_address",
}
# CSV write names with no mock function.
NO_MOCK_WRITE = {"refund_app_charge", "update_fulfillment"}

RESULT_FIELDS = [
    "ticket_id", "merchant_name", "shop_domain", "shop_in_store", "expected_route", "predicted_route",
    "routing", "should_escalate", "expected_writes", "executed_writes", "rejected_calls", "triage_error",
    "resolution", "resolution_note",
]

# ---- trace recording by wrapping the supervisor's functions ----
EVENTS: list[dict] = []
CURRENT = {"node": None}

_orig_chat = sv.chat
_orig_execute = sv.execute
_orig_triage = sv.triage
_orig_run_agent = sv.run_agent


def _chat(messages):
    try:
        text = _orig_chat(messages)
    except Exception as exc:
        EVENTS.append({"node": CURRENT["node"], "event": "llm_error", "messages_sent": len(messages),
                       "error": f"{type(exc).__name__}: {exc}"})
        raise
    EVENTS.append({"node": CURRENT["node"], "event": "llm_reply", "messages_sent": len(messages),
                   "raw": text[:2000]})
    return text


def _execute(agent, call, shop_domain):
    result = _orig_execute(agent, call, shop_domain)
    EVENTS.append({"node": CURRENT["node"], "event": "call", "tool": result.get("tool"),
                   "args": result.get("args"), "status": result.get("status"),
                   "message": result.get("message", "")})
    return result


def _triage(state):
    CURRENT["node"] = "triage"
    return _orig_triage(state)


def _run_agent(agent, ticket, shop_domain):
    CURRENT["node"] = f"{agent}_agent"
    return _orig_run_agent(agent, ticket, shop_domain)


# Patch before build_graph() so the graph picks up the wrapped functions.
sv.chat = _chat
sv.execute = _execute
sv.triage = _triage
sv.run_agent = _run_agent


def expected_writes(csv_tools: str) -> tuple[set, list]:
    names = [t.strip() for t in csv_tools.split(";") if t.strip()]
    unmapped = [t for t in names if t in NO_MOCK_WRITE]
    mapped = {ALIAS.get(t, t) for t in names}
    writes = {t for t in mapped if t in sv.REGISTRY and sv.REGISTRY[t]["mode"] == "WRITE"}
    return writes, unmapped


def score_resolution(row: dict, calls: list, shop_in_store: bool) -> tuple[str, str]:
    if not shop_in_store:
        return "not_evaluable", "shop not in fake store"
    exp, unmapped = expected_writes(row["expected_tool_calls"])
    done = {c["tool"] for c in calls if c.get("mode") == "WRITE" and c.get("status") == "ok"}
    if row["should_escalate"].strip().lower() == "true":
        if not done:
            return "pass", "no successful writes, as escalation requires"
        return "fail", f"successful writes on an escalation ticket: {sorted(done)}"
    if unmapped:
        return "not_evaluable", f"no mock function for {unmapped}"
    if done == exp:
        return "pass", f"writes match {sorted(exp)}"
    return "fail", f"expected writes {sorted(exp)}, executed {sorted(done)}"


def main() -> None:
    load_dotenv(ROOT / ".env")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY is not set (expected in .env)")
    sv.enable_langsmith()

    with sv.CSV_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    app = sv.build_graph()

    results = []
    for i, row in enumerate(rows):
        if i:
            time.sleep(PAUSE_S)
        tid = row["ticket_id"]
        shop = sv.SHOP_FOR_TICKET.get(tid, row["shop_domain"])
        shop_in_store = shop in sv.mock.FIXTURES
        sv.mock.reset_store()
        EVENTS.clear()
        CURRENT["node"] = None
        state = app.invoke(
            {"ticket": row, "shop_domain": shop, "decision": None, "error": "", "calls": []},
            config={"run_name": tid, "tags": ["eval", tid],
                    "metadata": {"ticket_id": tid, "expected_route": row["expected_route"]}},
        )

        predicted = state["decision"]["agent"] if state["decision"] else ""
        routing = "pass" if predicted == row["expected_route"] else "fail"
        resolution, note = score_resolution(row, state["calls"], shop_in_store)
        exp, _ = expected_writes(row["expected_tool_calls"])
        executed = sorted({c["tool"] for c in state["calls"] if c.get("mode") == "WRITE" and c.get("status") == "ok"})
        rejected = [f"{c.get('tool')}: {c.get('message', '')}" for c in state["calls"] if c.get("status") == "rejected"]

        trace_doc = {
            "ticket_id": tid,
            "merchant_name": row["merchant_name"],
            "shop_domain": shop,
            "shop_in_store": shop_in_store,
            "expected_route": row["expected_route"],
            "should_escalate": row["should_escalate"],
            "triage_error": state["error"],
            "decision": state["decision"],
            "routing": routing,
            "resolution": resolution,
            "resolution_note": note,
            "events": list(EVENTS),
        }
        (TRACE_DIR / f"{tid}.json").write_text(
            json.dumps(trace_doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

        results.append({
            "ticket_id": tid,
            "merchant_name": row["merchant_name"],
            "shop_domain": shop,
            "shop_in_store": shop_in_store,
            "expected_route": row["expected_route"],
            "predicted_route": predicted,
            "routing": routing,
            "should_escalate": row["should_escalate"],
            "expected_writes": ";".join(sorted(exp)),
            "executed_writes": ";".join(executed),
            "rejected_calls": " | ".join(rejected),
            "triage_error": state["error"],
            "resolution": resolution,
            "resolution_note": note,
        })
        print(f"{tid} routing={routing} predicted={predicted or '-'} expected={row['expected_route']} "
              f"resolution={resolution} ({note})", flush=True)

    with RESULTS_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(results)

    total = len(results)
    routed = sum(r["routing"] == "pass" for r in results)
    res_counts = {k: sum(r["resolution"] == k for r in results) for k in ("pass", "fail", "not_evaluable")}
    print(f"\nrouting: {routed}/{total} pass")
    print(f"resolution: pass={res_counts['pass']} fail={res_counts['fail']} not_evaluable={res_counts['not_evaluable']}")
    print(f"results: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
