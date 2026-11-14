"""One-step LangGraph flow: send a ticket's merchant message to a free OpenRouter model,
which returns a route (billing or orders) and a one-sentence reason.

Usage (from the repo root):
    .venv/bin/python agents/one_step.py                       # all tickets, writes out/*.jsonl
    .venv/bin/python agents/one_step.py --merchant "Maria Sousa"   # one ticket, prints route and reason

Reads OPENROUTER_API_KEY from .env. Set OPENROUTER_MODEL to override the model.
"""

import argparse
import csv
import json
import os
import pathlib
import time
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from openai import OpenAI, RateLimitError

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "Shopify Merchant Ticket Test Set (24 labelled tickets).csv"
MODEL = os.getenv("OPENROUTER_MODEL", "inclusionai/ling-3.2-flash:free")
# One results file per model so runs don't overwrite each other.
OUT_PATH = ROOT / "out" / f"one_step_{MODEL.replace('/', '_').replace(':', '_')}.jsonl"
ROUTES = {"billing", "orders"}

SYSTEM_PROMPT = (
    "You route Shopify merchant support tickets. The user message is the merchant's "
    "ticket text. Choose exactly one route. 'billing' covers charges, payouts, plan or "
    "app subscriptions, invoices, chargebacks and payment holds. 'orders' covers orders, "
    "fulfilment, shipping, addresses, and refunds or cancellations of orders. "
    "Reply with only a JSON object and nothing else: "
    '{"route": "billing" or "orders", "reason": "one sentence"}. '
    "The reason is neutral, third-person and in English. Do not address the merchant or "
    "the reader as 'you'. Do not invent order numbers, amounts, dates or facts that are "
    "not in the ticket."
)

MAX_ATTEMPTS = 6
PAUSE_BETWEEN_TICKETS_S = 4


class State(TypedDict):
    ticket_id: str
    message_body: str
    route: str
    reason: str
    error: str


def parse_route(text: str) -> tuple[str, str, str]:
    """Return (route, reason, error). Accepts a bare JSON object or one in a code fence."""
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return "", "", f"ParseError: {exc}; raw reply: {text[:200]}"
    route = str(data.get("route", "")).strip().lower()
    reason = str(data.get("reason", "")).strip()
    if route not in ROUTES:
        return "", reason, f"ParseError: route {route!r} is not billing or orders"
    if not reason:
        return route, "", "ParseError: empty reason"
    return route, reason, ""


def call_model(state: State) -> dict:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                max_tokens=1500,  # Ling spends most of a small budget on reasoning and returns no text
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": state["message_body"]},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                return {"route": "", "reason": "", "error": "EmptyReply: model returned no text"}
            route, reason, error = parse_route(text)
            return {"route": route, "reason": reason, "error": error}
        except RateLimitError as exc:
            if attempt == MAX_ATTEMPTS:
                return {"route": "", "reason": "", "error": f"RateLimitError after {attempt} attempts: {exc}"}
            time.sleep(min(60, 5 * 2 ** (attempt - 1)))  # 5, 10, 20, 40, 60s
        except Exception as exc:  # record other failures and keep going
            return {"route": "", "reason": "", "error": f"{type(exc).__name__}: {exc}"}
    return {"route": "", "reason": "", "error": "unreachable"}


def build_graph():
    graph = StateGraph(State)
    graph.add_node("call_model", call_model)
    graph.add_edge(START, "call_model")
    graph.add_edge("call_model", END)
    return graph.compile()


def load_tickets() -> list[dict]:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_one(app, row: dict) -> dict:
    result = app.invoke(
        {
            "ticket_id": row["ticket_id"],
            "message_body": row["message_body"],
            "route": "",
            "reason": "",
            "error": "",
        }
    )
    return {
        "ticket_id": row["ticket_id"],
        "merchant_name": row["merchant_name"],
        "model": MODEL,
        "route": result["route"],
        "reason": result["reason"],
        "expected_route": row["expected_route"],
        "error": result["error"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--merchant", help="run only the ticket from this merchant_name")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY is not set (expected in .env)")

    app = build_graph()
    tickets = load_tickets()

    if args.merchant:
        matches = [t for t in tickets if t["merchant_name"].strip().lower() == args.merchant.strip().lower()]
        if len(matches) != 1:
            raise SystemExit(f"Expected one ticket for {args.merchant!r}, found {len(matches)}")
        rec = run_one(app, matches[0])
        if rec["error"]:
            print(f"{rec['ticket_id']} ({rec['merchant_name']}) ERROR: {rec['error']}")
            raise SystemExit(1)
        print(f"ticket:          {rec['ticket_id']} ({rec['merchant_name']})")
        print(f"model:           {rec['model']}")
        print(f"route:           {rec['route']}")
        print(f"reason:          {rec['reason']}")
        print(f"expected_route:  {rec['expected_route']}")
        print(f"route matches expected: {rec['route'] == rec['expected_route']}")
        return

    OUT_PATH.parent.mkdir(exist_ok=True)
    ok = 0
    with OUT_PATH.open("w", encoding="utf-8") as out:
        for i, row in enumerate(tickets):
            if i:
                time.sleep(PAUSE_BETWEEN_TICKETS_S)
            rec = run_one(app, row)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if rec["error"]:
                print(f"{rec['ticket_id']}  ERROR  {rec['error'][:200]}")
            else:
                ok += 1
                print(f"{rec['ticket_id']}  {rec['route']:8} {rec['reason'][:160]}")

    print(f"\n{ok}/{len(tickets)} tickets routed by {MODEL}. Results: {OUT_PATH}")


if __name__ == "__main__":
    main()
