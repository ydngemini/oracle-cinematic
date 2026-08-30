-- 0084 — record what inference costs
--
-- billing_usage_events already metered four things: leads engaged, AI voice
-- minutes, closed transactions, captured media. It did not meter the one input
-- with an unbounded price attached.
--
-- Requests were capped — 20 per minute per agent, in rate_limiter — but SPEND
-- was not, and nothing recorded it. On a flat $299/month plan that meant the
-- first sign of a runaway conversation, or simply of one very heavy user, would
-- have been the provider's invoice. Not a billing incident: an invisible one.
--
-- Prompt and completion tokens stay separate because every provider prices them
-- differently, and summing them destroys the ratio that says whether the prompt
-- or the answer is the expensive half. That ratio is the actionable number: a
-- bloated prompt is a code fix, a long answer is a product decision.
--
-- Nothing here charges anyone. _STRIPE_REPORTED still contains only
-- lead_engaged, so these accrue locally — which is the entire point of the
-- module: the history has to exist before the pricing question can be answered
-- with measurements instead of guesses.
--
-- 0067 chose a CHECK over an enum specifically so this would be a one-line
-- edit. It is.

ALTER TABLE billing_usage_events
    DROP CONSTRAINT IF EXISTS billing_usage_events_metric_check;

ALTER TABLE billing_usage_events
    ADD CONSTRAINT billing_usage_events_metric_check CHECK (metric IN (
        'lead_engaged',
        'ai_voice_minute',
        'transaction_closed',
        'media_capture',
        'ai_prompt_tokens',
        'ai_completion_tokens'
    ));
