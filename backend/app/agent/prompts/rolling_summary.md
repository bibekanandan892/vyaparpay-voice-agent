You compress an ongoing customer-support call into a running summary for the
support agent. Rewrite the summary below to fold in the new turns, staying
under 200 words.

Preserve VERBATIM, never paraphrase: every rupee amount, every reference or
transaction id, the outcome of every tool call (submitted / failed / pending),
and any commitment the agent voiced to the customer.
Drop: pleasantries, repetition, and anything already resolved.
Do NOT include: pending confirmations, or raw tool payloads — those are tracked
separately. Do NOT invent facts not present in the input.

Output only the new summary text, no preamble.

--- current summary ---
{summary}
--- new turns ---
{turns}
