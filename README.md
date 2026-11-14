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

## Status

Early stage. The test set is the first artifact; the one-step flow is the first agent. An evaluation harness will follow.
