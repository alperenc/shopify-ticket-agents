"""Supervisor graph: triage makes a Pydantic route decision, then the named agent handles the ticket.

    triage --agent=billing--> billing_agent
           --agent=orders---> orders_agent

The admin functions come from "Mock Shopify Admin API tools for the merchant support agents.py"
(repo root). Each agent may call only its assigned functions. The model chooses the calls;
the harness checks each call's name, mode and arguments before running it.

Usage (from the repo root):
    .venv/bin/python agents/supervisor.py SUP-48213 SUP-48339
"""

import argparse
import csv
import importlib.util
import inspect
import json
import os
import pathlib
import sys
import time
from typing import Literal, Optional, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from openai import OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "Shopify Merchant Ticket Test Set (24 labelled tickets).csv"
MOCK_PATH = ROOT / "Mock Shopify Admin API tools for the merchant support agents.py"
OUT_DIR = ROOT / "out"
MODEL = os.getenv("OPENROUTER_MODEL", "inclusionai/ling-3.2-flash:free")
MAX_ROUNDS = 4

# Load the fake store module as-is (the file name has spaces, so import it by path).
_spec = importlib.util.spec_from_file_location("mock_admin_api_user", MOCK_PATH)
mock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock)

READ_TOOLS = {
    "get_shop", "get_order", "list_orders", "list_order_transactions",
    "calculate_refund", "get_delivery_profile", "get_app_subscriptions", "list_billing_charges",
}
WRITE_TOOLS = {
    "create_refund", "update_shipping_rate", "recalculate_order_shipping",
    "update_order_shipping_address", "cancel_order", "add_order_note",
    "cancel_app_subscription", "issue_billing_credit",
}
TOOL_NAMES = {
    name for name, obj in vars(mock).items()
    if inspect.isfunction(obj) and obj.__module__ == mock.__name__
    and not name.startswith("_") and name != "reset_store"
}
assert TOOL_NAMES == READ_TOOLS | WRITE_TOOLS and not (READ_TOOLS & WRITE_TOOLS), "every tool must be READ or WRITE, once"

REGISTRY = {}
for name in sorted(TOOL_NAMES):
    fn = getattr(mock, name)
    sig = inspect.signature(fn)
    REGISTRY[name] = {
        "fn": fn,
        "mode": "READ" if name in READ_TOOLS else "WRITE",
        "params": list(sig.parameters),
        "required": [p for p, v in sig.parameters.items() if v.default is inspect.Parameter.empty],
    }

AGENT_TOOLS = {
    "billing": ["get_app_subscriptions", "list_billing_charges", "cancel_app_subscription", "issue_billing_credit"],
    "orders": [
        "get_shop", "get_order", "list_orders", "list_order_transactions", "calculate_refund",
        "create_refund", "get_delivery_profile", "update_shipping_rate", "recalculate_order_shipping",
        "update_order_shipping_address", "cancel_order", "add_order_note",
    ],
}

# The fake store has four shops. These CSV tickets are mapped to the fixture that fits them.
SHOP_FOR_TICKET = {
    "SUP-48213": "azulejo-ceramica-lisboa.myshopify.com",  # Portugal shop with domestic and EU shipping rates
    "SUP-48339": "northwind-coffee-roasters.myshopify.com",  # shop with a duplicated app charge
}

TRIAGE_PROMPT = (
    "You route Shopify merchant support tickets to one agent. Reply with only a JSON object with exactly "
    'these keys: "agent" (exactly "billing" or "orders"), "reason" (one neutral sentence in English), and '
    '"confidence" (a number from 0 to 1). "billing" covers charges, payouts, plan or app subscriptions, '
    "invoices, chargebacks and payment holds. \"orders\" covers orders, fulfilment, shipping, addresses, "
    "and refunds or cancellations of orders. Do not address the merchant as 'you'. Do not invent facts."
)


class RouteDecision(BaseModel):
    """Root decision from triage. Extra keys and other agent names are rejected."""

    model_config = ConfigDict(extra="forbid")
    agent: Literal["billing", "orders"]
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class SupState(TypedDict):
    ticket: dict
    shop_domain: str
    decision: Optional[dict]
    error: str
    calls: list


def chat(messages: list[dict]) -> str:
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
    for attempt in range(1, 7):
        try:
            resp = client.chat.completions.create(model=MODEL, max_tokens=4000, messages=messages)  # reasoning models need headroom
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
            if attempt == 6:
                raise ValueError("EmptyReply: model returned no text")
            time.sleep(3)
        except RateLimitError:
            if attempt == 6:
                raise
            time.sleep(min(60, 5 * 2 ** (attempt - 1)))
    raise RuntimeError("unreachable")


def load_json(text: str):
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)


def triage(state: SupState) -> dict:
    try:
        text = chat([
            {"role": "system", "content": TRIAGE_PROMPT},
            {"role": "user", "content": state["ticket"]["message_body"]},
        ])
        decision = RouteDecision.model_validate_json(json.dumps(load_json(text)))
    except (ValueError, ValidationError) as exc:
        return {"decision": None, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    except Exception as exc:
        return {"decision": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"decision": decision.model_dump(), "error": ""}


def after_triage(state: SupState):
    return END if state["error"] else state["decision"]["agent"]


def agent_system_prompt(agent: str) -> str:
    lines = []
    for name in AGENT_TOOLS[agent]:
        spec = REGISTRY[name]
        optional = [p for p in spec["params"] if p not in spec["required"]]
        params = ", ".join(spec["required"] + [f"{p}=None" for p in optional])
        lines.append(f"- {name}({params}) [{spec['mode']}]")
    return (
        f"You are the {agent} agent for Shopify merchant tickets. You may call only these functions. "
        "Each is marked READ or WRITE:\n"
        + "\n".join(lines)
        + "\nEach round, reply with only a JSON object: {\"calls\": [{\"tool\": \"name\", \"args\": {...}}]}. "
        "Use the exact parameter names shown. Use ids and values from the ticket or from earlier results. "
        "Do not invent ids. Reply {\"calls\": []} when no more calls are needed."
    )


def ticket_context(ticket: dict, shop_domain: str) -> str:
    fields = {
        "ticket_id": ticket["ticket_id"],
        "shop_domain": shop_domain,
        "order_id": ticket["order_id"],
        "amount_in_question": ticket["amount_in_question"],
        "subject": ticket["subject"],
        "message_body": ticket["message_body"],
    }
    return json.dumps(fields, ensure_ascii=False)


def parse_plan(text: str):
    try:
        data = load_json(text)
    except json.JSONDecodeError as exc:
        return None, f"ParseError: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("calls"), list):
        return None, 'ParseError: expected {"calls": [...]}'
    return data["calls"], ""


def execute(agent: str, call, shop_domain: str) -> dict:
    tool = call.get("tool") if isinstance(call, dict) else None
    args = call.get("args", {}) if isinstance(call, dict) else {}
    entry = {"tool": tool, "args": args, "mode": None}
    if not isinstance(tool, str) or tool not in AGENT_TOOLS[agent]:
        return {**entry, "status": "rejected", "message": f"{tool!r} is not a function of the {agent} agent"}
    spec = REGISTRY[tool]
    entry["mode"] = spec["mode"]
    if not isinstance(args, dict):
        return {**entry, "status": "rejected", "message": "args must be an object"}
    unknown = set(args) - set(spec["params"])
    missing = set(spec["required"]) - set(args)
    if unknown or missing:
        return {**entry, "status": "rejected",
                "message": f"unknown args {sorted(unknown)}, missing args {sorted(missing)}"}
    if args.get("shop_domain", shop_domain) != shop_domain:
        return {**entry, "status": "rejected", "message": f"shop_domain must be {shop_domain!r} for this ticket"}
    try:
        result = spec["fn"](**args)
    except mock.ToolError as exc:
        return {**entry, "status": "rejected", "message": str(exc)}
    except TypeError as exc:
        return {**entry, "status": "rejected", "message": f"bad arguments: {exc}"}
    return {**entry, "status": "ok", "result": result}


def run_agent(agent: str, ticket: dict, shop_domain: str) -> list[dict]:
    messages = [
        {"role": "system", "content": agent_system_prompt(agent)},
        {"role": "user", "content": ticket_context(ticket, shop_domain)},
    ]
    log: list[dict] = []
    for rnd in range(1, MAX_ROUNDS + 1):
        try:
            text = chat(messages)
        except Exception as exc:
            log.append({"round": rnd, "status": "error", "message": f"{type(exc).__name__}: {exc}"})
            break
        calls, err = parse_plan(text)
        if err:
            log.append({"round": rnd, "status": "error", "message": err, "raw": text[:200]})
            break
        if not calls:
            break
        results = []
        for call in calls:
            entry = {"round": rnd, **execute(agent, call, shop_domain)}
            log.append(entry)
            results.append(entry)
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": "Results: " + json.dumps(results, ensure_ascii=False, default=str)})
    return log


def make_agent_node(agent: str):
    def node(state: SupState) -> dict:
        return {"calls": run_agent(agent, state["ticket"], state["shop_domain"])}

    return node


def build_graph():
    graph = StateGraph(SupState)
    graph.add_node("triage", triage)
    graph.add_node("billing_agent", make_agent_node("billing"))
    graph.add_node("orders_agent", make_agent_node("orders"))
    graph.add_edge(START, "triage")
    graph.add_conditional_edges("triage", after_triage,
                                {"billing": "billing_agent", "orders": "orders_agent", END: END})
    graph.add_edge("billing_agent", END)
    graph.add_edge("orders_agent", END)
    return graph.compile()


def print_report(ticket: dict, shop_domain: str, state: dict) -> None:
    print(f"\n{ticket['ticket_id']} ({ticket['merchant_name']}) on fake store shop {shop_domain}")
    if state["error"]:
        print(f"  triage ERROR: {state['error']}")
        return
    d = state["decision"]
    print(f"  decision: agent={d['agent']} reason={d['reason']!r} confidence={d['confidence']}")
    print(f"  CSV expected_route: {ticket['expected_route']} (match: {d['agent'] == ticket['expected_route']})")
    print(f"  CSV expected_tool_calls: {ticket['expected_tool_calls'] or '-'}")
    if not state["calls"]:
        print("  calls: none")
    for c in state["calls"]:
        if "tool" not in c:
            print(f"  [{c['round']}] {c['status']}: {c['message']}")
            continue
        args = json.dumps(c["args"], ensure_ascii=False)
        print(f"  [{c['round']}] {c['mode'] or '-':5} {c['tool']}({args}) -> {c['status']}")
        detail = c["message"] if c["status"] == "rejected" else json.dumps(c["result"], ensure_ascii=False, default=str)
        print(f"        {detail[:300]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticket_ids", nargs="+", help="ticket IDs, for example SUP-48213")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY is not set (expected in .env)")

    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        tickets = {r["ticket_id"]: r for r in csv.DictReader(f)}
    OUT_DIR.mkdir(exist_ok=True)
    app = build_graph()
    for tid in args.ticket_ids:
        if tid not in tickets or tid not in SHOP_FOR_TICKET:
            raise SystemExit(f"no shop mapping or ticket for {tid!r}")
        mock.reset_store()
        print(f"reset_store() called before {tid}")
        ticket = tickets[tid]
        shop_domain = SHOP_FOR_TICKET[tid]
        state = app.invoke({"ticket": ticket, "shop_domain": shop_domain, "decision": None,
                            "error": "", "calls": []})
        print_report(ticket, shop_domain, state)
        (OUT_DIR / f"supervisor_{tid}.json").write_text(
            json.dumps({k: v for k, v in state.items() if k != "ticket"}, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
