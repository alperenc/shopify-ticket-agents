# shopify-ticket-agents

Agents that resolve Shopify merchant support tickets without a human in the loop, with a clear hand-off to a human advisor when a ticket should not be automated.

## Test set

`Shopify Merchant Ticket Test Set (24 labelled tickets).csv` is a labelled evaluation set of 24 merchant tickets (SUP-48197 to SUP-48577). Each row gives the ticket, the expected route, the Shopify tools an agent should call, the expected fix, and whether the ticket is a two-problem case and whether it should escalate.

| Column | Meaning |
| --- | --- |
| `ticket_id` | Support ticket ID |
| `created_at` | Ticket creation time (UTC) |
| `merchant_name`, `shop_domain`, `merchant_country` | Merchant context |
| `subject`, `message_body` | What the merchant wrote |
| `order_id` | Shopify order GID or order reference, when one is involved |
| `amount_in_question` | Money at stake, with currency |
| `expected_route` | Queue the ticket belongs in: `billing` or `orders` |
| `expected_tool_calls` | Semicolon-separated Shopify tools an agent is expected to use |
| `expected_fix` | The action that resolves the ticket |
| `is_two_problem` | `true` when the message contains two separate issues |
| `should_escalate` | `true` when the ticket must go to a human advisor |

Tickets marked `should_escalate = true` cover cases such as chargeback disputes, bulk cancellations, refunds above the agent's limit, and payout holds. These are expected to be handed to a human, not closed by an agent.

## One-step flow

`agents/one_step.py` is a single-node LangGraph graph. It sends one ticket's `message_body` to a free model on OpenRouter and prints the route (`billing` or `orders`) and a one-sentence reason.

Setup:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'OPENROUTER_API_KEY=...' > .env   # not committed; .env is in .gitignore
```

Run one ticket by merchant name, or all 24 (results go to `out/`, which is ignored by git):

```bash
.venv/bin/python agents/one_step.py --merchant "Maria Sousa"
.venv/bin/python agents/one_step.py
```

The default model is `inclusionai/ling-3.2-flash:free`. Set `OPENROUTER_MODEL` to change it. Free models are rate-limited per minute and per day; a run that hits the daily cap records errors for the tickets it could not answer.

### Prompt

```text
You route Shopify merchant support tickets. The user message is the merchant's ticket text. Choose exactly one route. 'billing' covers charges, payouts, plan or app subscriptions, invoices, chargebacks and payment holds. 'orders' covers orders, fulfilment, shipping, addresses, and refunds or cancellations of orders. Reply with only a JSON object and nothing else: {"route": "billing" or "orders", "reason": "one sentence"}. The reason is neutral, third-person and in English. Do not address the merchant or the reader as 'you'. Do not invent order numbers, amounts, dates or facts that are not in the ticket.
```

### Run output: Maria Sousa (SUP-48213)

```text
ticket:          SUP-48213 (Maria Sousa)
model:           inclusionai/ling-3.2-flash:free
route:           orders
reason:          The merchant reports incorrect shipping rates applied to orders, with domestic and international postage charges reversed across multiple orders.
expected_route:  orders
route matches expected: True
```

Route check: SUP-48213 routed to `orders`, matching `expected_route` `orders`.

The reason states the swap from the merchant's message: buyers in Europe were charged the Portuguese domestic rate, and buyers in Portugal were charged international rates. It does not mention the Braga customer example.

### Run output: all 24 tickets

Route check across the full test set: 24 of 24 routes match `expected_route`, with no errors. Results are in `out/one_step_inclusionai_ling-3.2-flash_free.jsonl`, which is ignored by git.

## Supervisor graph

`agents/supervisor.py` runs a LangGraph supervisor. Triage returns a Pydantic `RouteDecision` (`agent`, `reason`, `confidence`), and the graph sends the ticket to the agent it names. Each agent sees only its assigned functions from `Mock Shopify Admin API tools for the merchant support agents.py`, and the model chooses the calls. The harness checks each call's name, arguments and shop before running it, and records the result as `ok` or `rejected`.

The fake store has four shops, and neither CSV shop domain is among them. Maria's ticket is mapped to `azulejo-ceramica-lisboa.myshopify.com`, the Portugal fixture. The billing ticket SUP-48339 is mapped to `northwind-coffee-roasters.myshopify.com`, the fixture with a duplicated app charge. Its duplicate is $19.99, not the ticket's $105.

Run it with a reset before each ticket:

```bash
.venv/bin/python agents/supervisor.py SUP-48213
.venv/bin/python agents/supervisor.py SUP-48339
```

### Admin functions and agents

Each function is assigned to exactly one agent. `reset_store()` is the harness reset, not an agent function.

| Function | Parameters | Mode | Agent |
| --- | --- | --- | --- |
| `calculate_refund` | shop_domain, order_id, amount | READ | orders |
| `get_app_subscriptions` | shop_domain | READ | billing |
| `get_delivery_profile` | shop_domain | READ | orders |
| `get_order` | shop_domain, order_id | READ | orders |
| `get_shop` | shop_domain | READ | orders |
| `list_billing_charges` | shop_domain, billing_period | READ | billing |
| `list_order_transactions` | shop_domain, order_id | READ | orders |
| `list_orders` | shop_domain, created_on_min, created_on_max, shipping_rate_title | READ | orders |
| `add_order_note` | shop_domain, order_id, note | WRITE | orders |
| `cancel_app_subscription` | shop_domain, subscription_id | WRITE | billing |
| `cancel_order` | shop_domain, order_id, reason | WRITE | orders |
| `create_refund` | shop_domain, order_id, amount, reason, notify_customer | WRITE | orders |
| `issue_billing_credit` | shop_domain, amount, description, charge_id | WRITE | billing |
| `recalculate_order_shipping` | shop_domain, order_id | WRITE | orders |
| `update_order_shipping_address` | shop_domain, order_id, address | WRITE | orders |
| `update_shipping_rate` | shop_domain, rate_id, price | WRITE | orders |

### Triage prompt

```text
You route Shopify merchant support tickets to one agent. Reply with only a JSON object with exactly these keys: "agent" (exactly "billing" or "orders"), "reason" (one neutral sentence in English), and "confidence" (a number from 0 to 1). "billing" covers charges, payouts, plan or app subscriptions, invoices, chargebacks and payment holds. "orders" covers orders, fulfilment, shipping, addresses, and refunds or cancellations of orders. Do not address the merchant as 'you'. Do not invent facts.
```

### Agent prompt (billing example)

The orders agent gets the same template with its own function list.

```text
You are the billing agent for Shopify merchant tickets. You may call only these functions. Each is marked READ or WRITE:
- get_app_subscriptions(shop_domain) [READ]
- list_billing_charges(shop_domain, billing_period=None) [READ]
- cancel_app_subscription(shop_domain, subscription_id) [WRITE]
- issue_billing_credit(shop_domain, amount, description, charge_id=None) [WRITE]
Each round, reply with only a JSON object: {"calls": [{"tool": "name", "args": {...}}]}. Use the exact parameter names shown. Use ids and values from the ticket or from earlier results. Do not invent ids. Reply {"calls": []} when no more calls are needed.
```

### Run output: SUP-48213 (Maria Sousa)

```text
reset_store() called before SUP-48213

SUP-48213 (Maria Sousa) on fake store shop azulejo-ceramica-lisboa.myshopify.com
  decision: agent=orders reason='The report describes incorrect domestic and international shipping rates being applied to roughly 40 orders, including an overcharged customer in Braga, which falls under shipping and fulfilment.' confidence=0.95
  CSV expected_route: orders (match: True)
  CSV expected_tool_calls: get_shop;get_shipping_profile;update_shipping_rate;list_orders
  [1] READ  get_shop({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com"}) -> ok
        {"name": "Azulejo Cerâmica Lisboa", "domain": "azulejo-ceramica-lisboa.myshopify.com", "owner": "Maria Santos", "country": "PT", "currency": "EUR", "plan": "Basic", "timezone": "Europe/Lisbon"}
  [1] READ  get_delivery_profile({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com"}) -> ok
        {"id": "dp_azulejo_general", "name": "General profile", "origin": "Rua de São Bento 212, 1200-821 Lisboa, PT", "zones": [{"id": "zone_pt", "name": "Portugal", "countries": ["PT"], "rates": [{"id": "rate_pt_ctt", "name": "CTT Domestic", "price": 4.9, "currency": "EUR", "updated_at": "2024-03-11T09:14
  [1] READ  list_orders({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com"}) -> ok
        [{"order_id": "#1158", "created_at": "2024-11-28T17:22:00+00:00", "customer_name": "Mathilde Krause", "destination_country": "DE", "shipping_title": "EU Standard", "shipping_price": 14.9, "total_price": 72.9, "currency": "EUR", "financial_status": "paid", "fulfillment_status": "fulfilled"}, {"order_
  [2] READ  get_order({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com", "order_id": "#1158"}) -> ok
        {"order_id": "#1158", "created_at": "2024-11-28T17:22:00+00:00", "customer": {"name": "Mathilde Krause"}, "line_items": [{"title": "Hand-painted serving platter", "quantity": 1, "price": 58.0}], "subtotal_price": 58.0, "shipping_lines": [{"title": "EU Standard", "price": 14.9, "rate_id": "rate_eu_st
  [2] READ  get_order({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com", "order_id": "#1200"}) -> ok
        {"order_id": "#1200", "created_at": "2024-12-03T10:05:00+00:00", "customer": {"name": "Rui Carvalho"}, "line_items": [{"title": "Sardine wall plate", "quantity": 2, "price": 24.5}], "subtotal_price": 49.0, "shipping_lines": [{"title": "CTT Domestic", "price": 4.9, "rate_id": "rate_pt_ctt", "currency
  [2] READ  list_order_transactions({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com", "order_id": "#1158"}) -> ok
        [{"id": "txn_1158_1", "kind": "sale", "status": "success", "amount": 72.9, "currency": "EUR", "gateway": "shopify_payments", "processed_at": "2024-11-28T17:22:00+00:00"}]
  [2] READ  list_order_transactions({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com", "order_id": "#1200"}) -> ok
        [{"id": "txn_1200_1", "kind": "sale", "status": "success", "amount": 53.9, "currency": "EUR", "gateway": "shopify_payments", "processed_at": "2024-12-03T10:05:00+00:00"}]
  [3] READ  get_order({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com", "order_id": "#1159"}) -> rejected
        Order '#1159' not found on azulejo-ceramica-lisboa.myshopify.com. Order IDs include the '#', e.g. '#1187'.
  [3] READ  list_orders({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com", "shipping_rate_title": "CTT Domestic"}) -> ok
        [{"order_id": "#1200", "created_at": "2024-12-03T10:05:00+00:00", "customer_name": "Rui Carvalho", "destination_country": "PT", "shipping_title": "CTT Domestic", "shipping_price": 4.9, "total_price": 53.9, "currency": "EUR", "financial_status": "paid", "fulfillment_status": "unfulfilled"}]
  [3] READ  list_orders({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com", "shipping_rate_title": "EU Standard"}) -> ok
        [{"order_id": "#1158", "created_at": "2024-11-28T17:22:00+00:00", "customer_name": "Mathilde Krause", "destination_country": "DE", "shipping_title": "EU Standard", "shipping_price": 14.9, "total_price": 72.9, "currency": "EUR", "financial_status": "paid", "fulfillment_status": "fulfilled"}, {"order_
  [4] WRITE update_shipping_rate({"shop_domain": "azulejo-ceramica-lisboa.myshopify.com", "rate_id": "rate_eu_standard", "price": 14.9}) -> ok
        {"id": "rate_eu_standard", "name": "EU Standard", "price": 14.9, "currency": "EUR", "updated_at": "2026-11-14T20:50:26+00:00", "zone": "European Union", "previous_price": 0.0}
maria exit 0
```

Checked: the decision is `orders` with reason and confidence filled, and every call is in the orders agent's list. The reads match the Portugal fixture, and the one write sets `rate_eu_standard` from 0.00 to 14.90, matching the fixture's previous price. The 14.90 is the model's inference from order #1158, not a rate from the ticket.

### Run output: SUP-48339 (Bethany Coyle)

```text
reset_store() called before SUP-48339

SUP-48339 (Bethany Coyle) on fake store shop northwind-coffee-roasters.myshopify.com
  decision: agent=billing reason='The report primarily concerns duplicate pending and posted Shopify plan charges on December 1 and a request to refund one of them, with a secondary mention of an unfulfilled order.' confidence=0.85
  CSV expected_route: billing (match: True)
  CSV expected_tool_calls: get_billing_invoice;apply_billing_credit;get_order;get_fulfillment;update_fulfillment
  [1] READ  list_billing_charges({"shop_domain": "northwind-coffee-roasters.myshopify.com"}) -> ok
        [{"id": "chg_nw_5501", "type": "subscription", "description": "Shopify plan", "subscription_id": null, "amount": 105.0, "currency": "USD", "billed_on": "2024-11-30", "billing_period": "2024-11"}, {"id": "chg_nw_5502", "type": "app_subscription", "description": "ReviewStream Product Reviews - Growth"
  [1] READ  get_app_subscriptions({"shop_domain": "northwind-coffee-roasters.myshopify.com"}) -> ok
        [{"id": "sub_rvw_2207", "app_name": "ReviewStream Product Reviews", "plan": "Growth", "price": 19.99, "currency": "USD", "status": "active", "activated_on": "2024-05-14", "cancelled_on": null}]
  [2] READ  list_billing_charges({"shop_domain": "northwind-coffee-roasters.myshopify.com", "billing_period": "2024-12"}) -> ok
        []
  [2] READ  list_billing_charges({"shop_domain": "northwind-coffee-roasters.myshopify.com", "billing_period": "2024-11"}) -> ok
        [{"id": "chg_nw_5501", "type": "subscription", "description": "Shopify plan", "subscription_id": null, "amount": 105.0, "currency": "USD", "billed_on": "2024-11-30", "billing_period": "2024-11"}, {"id": "chg_nw_5502", "type": "app_subscription", "description": "ReviewStream Product Reviews - Growth"
  [3] WRITE issue_billing_credit({"shop_domain": "northwind-coffee-roasters.myshopify.com", "amount": 19.99, "description": "Duplicate app subscription charge: ReviewStream Product Reviews - Growth (sub_rvw_2207) was billed twice in the 2024-11 period. Crediting duplicate charge chg_nw_5503; chg_nw_5502 is the valid charge. Only one Shopify plan charge of $105 (chg_nw_5501) exists, so no $105 duplicate was found.", "charge_id": "chg_nw_5503"}) -> ok
        {"id": "crd_d547b040b4", "type": "credit", "description": "Duplicate app subscription charge: ReviewStream Product Reviews - Growth (sub_rvw_2207) was billed twice in the 2024-11 period. Crediting duplicate charge chg_nw_5503; chg_nw_5502 is the valid charge. Only one Shopify plan charge of $105 (ch
billing exit 0
```

Checked: the decision is `billing` with reason and confidence filled, and every call is in the billing agent's list. The credit of 19.99 is within charge `chg_nw_5503`, the fixture's duplicate, and the agent credited the fixture's duplicate rather than the ticket's $105.

## Eval

`eval/run_eval.py` loads `shopify_merchant_tickets_test_set.csv`, runs all 24 tickets through the billing and orders supervisor graph with LangSmith tracing on, and writes `eval/results.csv` (one row per `ticket_id`). Run it from the repo root:

```bash
.venv/bin/python eval/run_eval.py
```

Columns: `predicted_route`, `tools_called`, `final_status`, `routing_pass`, `resolution_pass`. LangSmith tracing turns on when `LANGSMITH_API_KEY` is in `.env`. Also set `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`, `LANGSMITH_AGENT_ID=shopify-ticket-agents` and `LANGSMITH_AGENT_ENVIRONMENT=local`. Do not set `LANGSMITH_PROJECT`, because the SDK errors when a project and an agent address are both set. Runs land in the `shopify-ticket-agents-local` project, named by ticket ID.

## Baseline eval

Baseline from `eval/results.csv`: **2 of 24** tickets pass both routing and resolution (SUP-48213 and SUP-48361). Routing is 24 of 24. Resolution is 2 of 24.

Scoring request:

```text
Grade routing_pass against expected_route. Grade resolution_pass against expected_tool_calls, read together with expected_fix, using this rule: every expected tool name must be called, and the order_id and amount arguments must match the ticket's order_id and amount_in_question; tool order does not matter. Keep routing_pass and resolution_pass as two separate columns. If a ticket makes no tool call or the run errors, still write its row, say so in final_status, and mark it fail.
```

**Resolution rule.** Every name in `expected_tool_calls` must appear in the run (CSV names map onto the mock API: `apply_billing_credit` → `issue_billing_credit`, `get_billing_invoice` / `list_app_charges` → `list_billing_charges`, `get_shipping_profile` → `get_delivery_profile`, `update_order_address` → `update_order_shipping_address`). Extra calls are allowed. Tool order does not matter. When a matching tool takes `order_id` or `amount`, those arguments must equal the ticket's `order_id` and `amount_in_question`. A run with no tool calls, or a run that errors, is fail.

SUP-48339 is why those arguments are scored. `expected_fix` is to credit the duplicate **USD 105.00** Grow plan charge and mark order 1290 fulfilled (`order_id` `gid://shopify/Order/5938471100263`). The run called `issue_billing_credit` with **amount 19.99** on fixture charge `chg_nw_5503`. Name-only scoring would count that as `apply_billing_credit`. The 19.99 is the fixture's ReviewStream duplicate, not the ticket amount.

On `expected_fix`: reading it with `expected_tool_calls` is why the scorer maps those CSV names onto the mock API, and why `order_id` and `amount` are checked (the fix names those ticket fields). Every expected tool name is still required; tool order is still ignored.

### Fail rows checked against LangSmith

Two fail rows from this run, checked against the CSV and the traces in `shopify-ticket-agents-local`:

| | CSV row | LangSmith run |
| --- | --- | --- |
| **SUP-48339** (fail) | routing pass (`billing`); expected `get_billing_invoice;apply_billing_credit;get_order;get_fulfillment;update_fulfillment`; `amount_in_question` USD 105.00; `order_id` `gid://shopify/Order/5938471100263`; `tools_called` `get_app_subscriptions;list_billing_charges;issue_billing_credit`; resolution fail | Run `01a1e493-5c1e-71cc-b77c-afab88d48793`: triage `billing`; 3 admin calls, all ok (`get_app_subscriptions`, `list_billing_charges`, `issue_billing_credit` of **19.99** on `chg_nw_5503`). Matches the CSV. Missing `get_order`, `get_fulfillment`, `update_fulfillment`. The credit amount is 19.99, not 105.00. |
| **SUP-48478** (fail) | routing pass (`orders`); expected `get_order;create_refund`; `order_id` `gid://shopify/Order/5944906251378`; `amount_in_question` USD 25.00; `tools_called` `get_order;calculate_refund;get_shop;get_shop;get_order;list_orders;get_order`; resolution fail | Run `01a1e4a4-7f70-7532-a9c9-b6cc6f9373a1`: 7 admin calls, all rejected (shop not in the fake store). `get_order` and `calculate_refund` used the ticket GID and amount 25.0. `create_refund` was never called. Matches the CSV. |

### Fails by cause

22 tickets fail resolution. None fail routing.

- **Wrong route:** none.
- **Wrong tool:** SUP-48478 (`calculate_refund` in place of `create_refund`).
- **Wrong argument:** none as a sole cause. No fail had every expected tool name present. SUP-48339's credit was also the wrong amount (19.99 vs 105.00).
- **Missing tool:** the other 21. Lookups without the expected write: SUP-48197, SUP-48258, SUP-48279, SUP-48302, SUP-48412, SUP-48531, SUP-48577 (`refund_app_charge` or `apply_billing_credit`); SUP-48241, SUP-48377 (`update_order_address`); SUP-48226, SUP-48266, SUP-48346, SUP-48436 (`get_fulfillment` / `update_fulfillment`); SUP-48288 (`create_refund`); SUP-48327 (`cancel_order`); SUP-48339 (`get_order`, `get_fulfillment`, `update_fulfillment`). No tool calls: SUP-48204, SUP-48455 (`get_shop;get_payout`); escalate tickets SUP-48315 and SUP-48502 (empty expected list; the scorer still fails a run with no calls). SUP-48394 errored (`ParseError`) after `get_order;list_order_transactions;get_shop`.

## Status

Early stage. The test set is the first artifact; the one-step flow and the supervisor graph are the first agents, and the eval gives the first baseline.
