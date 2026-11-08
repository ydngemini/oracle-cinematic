// Node's own test runner (no toolchain in this app): node --test src/lib/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { budgetRefusal, copilotError } from './copilotError.js';

const json = (status, body) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

const EXHAUSTED = "This month's Neoh AI budget for your brokerage is used up, so Neoh stopped before asking a model.";

test('a 402 budget refusal is said in the words the server chose', async () => {
  const error = await copilotError(json(402, {
    detail: { code: 'AI_BUDGET_EXHAUSTED', message: EXHAUSTED, state: 'exhausted' },
  }));
  assert.equal(error.status, 402);
  assert.equal(error.detail.code, 'AI_BUDGET_EXHAUSTED');
  assert.equal(budgetRefusal(error), EXHAUSTED);
});

test('a 503 carrying a budget state (halted, unreadable) is a refusal too', async () => {
  for (const state of ['platform_halted', 'unknown']) {
    const error = await copilotError(json(503, {
      detail: { code: 'AI_SPEND_HALTED', message: `stopped: ${state}`, state },
    }));
    assert.equal(budgetRefusal(error), `stopped: ${state}`);
  }
});

test('a 402 without a message still says the AI budget stopped it', async () => {
  const error = await copilotError(json(402, { detail: { code: 'AI_SPEND_PAUSED', state: 'paused' } }));
  assert.match(budgetRefusal(error), /AI budget/);
});

test('a model the server could not reach is not a refusal: the rule-based responder answers', async () => {
  const error = await copilotError(json(503, { reply: null, error: 'llm_unavailable' }));
  assert.equal(error.status, 503);
  assert.equal(budgetRefusal(error), null);
});

test('a body that is not JSON, or no response at all, is not a refusal', async () => {
  const error = await copilotError(new Response('<html>bad gateway</html>', { status: 502 }));
  assert.equal(error.status, 502);
  assert.equal(error.detail, null);
  assert.equal(budgetRefusal(error), null);
  assert.equal(budgetRefusal(new TypeError('Failed to fetch')), null);
  assert.equal(budgetRefusal(undefined), null);
});
