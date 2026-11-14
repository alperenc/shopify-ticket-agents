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

## Status

Early stage. The test set is the first artifact; agents and an evaluation harness will follow.
