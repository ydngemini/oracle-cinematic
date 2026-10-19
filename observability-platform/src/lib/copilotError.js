// Why the ops copilot did not answer (T18.07). No imports, so Node's own test
// runner can load it (node --test "src/lib/*.test.js").
//
// POST /api/aws/copilot/query refuses with 402, or with 503 and a budget
// state, when the brokerage's AI budget or the platform's AI switch stopped
// the question: {detail: {code, message, state}} (backend
// ai_budget.http_refusal). That is said as the server words it. The
// rule-based responder's answer in its place would read as the model's, and
// the owner would never learn the budget stopped it. Anything else (the model
// unreachable: 503 {error: 'llm_unavailable'}, or no response at all) keeps
// the rule-based fallback.

const BUDGET_STATES = new Set(['exhausted', 'insufficient', 'paused', 'platform_halted', 'unknown']);
const BUDGET_STOPPED = "Your brokerage's AI budget stopped this question.";

// An Error for a failed copilot response, carrying its HTTP status and the
// server's detail (null when the body is not JSON).
export async function copilotError(res) {
  let body;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  const error = new Error(`copilot -> ${res.status}`);
  error.status = res.status;
  error.detail = body && typeof body === 'object' && 'detail' in body ? body.detail : body;
  return error;
}

// What the copilot answers a question with: the model's reply; the
// server's sentence when the AI budget stopped the question; otherwise (the
// model unreachable, an empty reply) the rule-based responder's answer.
// `ask` is the request (fetchCopilotReply), `ruleBased` the fallback.
export async function copilotAnswer(ask, ruleBased) {
  try {
    const { reply } = await ask();
    return reply || ruleBased();
  } catch (error) {
    return budgetRefusal(error) || ruleBased();
  }
}

// The sentence to show when the AI budget stopped the question, or null.
export function budgetRefusal(error) {
  const detail = error && error.detail && typeof error.detail === 'object' ? error.detail : null;
  const status = error?.status;
  const refused = status === 402 || (status === 503 && BUDGET_STATES.has(detail?.state));
  if (!refused) return null;
  const message = typeof detail?.message === 'string' ? detail.message.trim() : '';
  return message || BUDGET_STOPPED;
}
