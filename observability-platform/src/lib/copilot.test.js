// The ops copilot says a budget refusal (T18.07), end to end: fetchCopilotReply
// throws it carrying its status and the server's detail, and AICopilot answers
// with the server's sentence, never with the rule-based responder in its place.
// Node's own test runner (no toolchain in this app): node --test src/lib/
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { fetchCopilotReply } from './api.js';
import { copilotAnswer } from './copilotError.js';

const EXHAUSTED = "This month's Neoh AI budget for your brokerage is used up, so Neoh stopped before asking a model.";
const RULES = 'rule-based answer';

const respond = (status, body) => async () => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

const ask = () => copilotAnswer(
  () => fetchCopilotReply('why is the database slow?', { counts: {} }),
  () => RULES,
);

beforeEach(() => {
  globalThis.fetch = undefined;
});

test('fetchCopilotReply throws a budget refusal with its status and the server detail', async () => {
  globalThis.fetch = respond(402, {
    detail: { code: 'AI_BUDGET_EXHAUSTED', message: EXHAUSTED, state: 'exhausted' },
  });
  await assert.rejects(fetchCopilotReply('why?', {}), (error) => {
    assert.equal(error.status, 402);
    assert.deepEqual(error.detail, { code: 'AI_BUDGET_EXHAUSTED', message: EXHAUSTED, state: 'exhausted' });
    return true;
  });
});

test('a refused question is answered with the refusal, not the rule-based responder', async () => {
  globalThis.fetch = respond(402, {
    detail: { code: 'AI_BUDGET_EXHAUSTED', message: EXHAUSTED, state: 'exhausted' },
  });
  assert.equal(await ask(), EXHAUSTED);

  globalThis.fetch = respond(503, {
    detail: { code: 'AI_BUDGET_UNAVAILABLE', message: 'Try again in a moment.', state: 'unknown' },
  });
  assert.equal(await ask(), 'Try again in a moment.');
});

test('an unreachable model, or no response at all, keeps the rule-based responder', async () => {
  globalThis.fetch = respond(503, { reply: null, error: 'llm_unavailable' });
  assert.equal(await ask(), RULES);

  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  assert.equal(await ask(), RULES);
});

test("the model's answer is shown, and an empty one falls back to the rule-based responder", async () => {
  globalThis.fetch = respond(200, { reply: 'RDS CPU is at 92%.', model: 'm' });
  assert.equal(await ask(), 'RDS CPU is at 92%.');

  globalThis.fetch = respond(200, { reply: '', model: 'm' });
  assert.equal(await ask(), RULES);
});

test('AICopilot answers every question through copilotAnswer, with no fallback of its own', () => {
  // The component is JSX (no toolchain here to render it), so its wiring is
  // read: the send handler must take its answer from copilotAnswer — asking
  // fetchCopilotReply, falling back to the rule-based responder only through
  // it — and must not catch a failure itself, where the rule-based answer
  // would again stand in for a refusal.
  const source = readFileSync(new URL('../components/AICopilot.jsx', import.meta.url), 'utf8');
  const start = source.indexOf('const handleChatSubmit');
  const end = source.indexOf('const handleActionClick');
  assert.ok(start !== -1 && end > start, 'the send handler was not found');
  const handler = source.slice(start, end);
  assert.match(source, /import\s*\{[^}]*\bcopilotAnswer\b[^}]*\}\s*from\s*'\.\.\/lib\/copilotError(\.js)?'/);
  assert.match(handler, /await copilotAnswer\(\s*\(\)\s*=>\s*fetchCopilotReply\(/);
  assert.match(handler, /\(\)\s*=>\s*generateCopilotResponse\(/);
  assert.doesNotMatch(handler, /\bcatch\b/);
});
