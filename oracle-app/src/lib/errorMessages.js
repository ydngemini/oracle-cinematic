/**
 * One product-language path for every error a customer can see.
 *
 *   friendlyError(err)                                   → sentence
 *   friendlyError(err, { action: 'load your contacts' }) → "Couldn't load your contacts. …"
 *   friendlyError(err, { capability: 'Calling' })        → "Calling is temporarily unavailable. The rest of Neoh still works."
 *   describeError(err, opts)                             → { message, retryable, technical }
 *
 * The raw server text (codes, env flags, provider names, stack fragments) is
 * never the headline. It is returned as `technical` for a collapsed detail or
 * the console. A server message is passed through only when it already reads
 * like a sentence meant for a person (validation feedback such as
 * "That email is already invited.").
 */

export const ERROR_MESSAGES = {
  NETWORK_ERROR: "Couldn't reach Neoh. Check your connection and try again.",
  TIMEOUT: 'That took too long to respond. Try again.',
  SERVER_ERROR: "Neoh couldn't finish that just now. Try again in a moment.",
  // 502/503/504 — in production the DigitalOcean edge turns every backend 503
  // into an HTML 504, so the status is all we can rely on.
  UNAVAILABLE: 'Neoh is temporarily unavailable. Your saved work is safe — try again in a moment.',
  UNAUTHORIZED: 'Your session ended. Sign in again to continue.',
  FORBIDDEN: "You don't have access to do that. Ask your broker if you need it.",
  NOT_FOUND: "We couldn't find that. It may have been moved or removed.",
  CONFLICT: 'That changed while you were working on it. Refresh and try again.',
  TOO_LARGE: 'That file is too large to upload.',
  VALIDATION_ERROR: 'Some details need another look. Check them and try again.',
  RATE_LIMITED: 'Too many requests at once. Wait a moment and try again.',
  DEFAULT: 'Something went wrong. Try again.',
};

const STATUS_MESSAGES = {
  400: ERROR_MESSAGES.VALIDATION_ERROR,
  401: ERROR_MESSAGES.UNAUTHORIZED,
  403: ERROR_MESSAGES.FORBIDDEN,
  404: ERROR_MESSAGES.NOT_FOUND,
  408: ERROR_MESSAGES.TIMEOUT,
  409: ERROR_MESSAGES.CONFLICT,
  413: ERROR_MESSAGES.TOO_LARGE,
  422: ERROR_MESSAGES.VALIDATION_ERROR,
  429: ERROR_MESSAGES.RATE_LIMITED,
};

// Anything that smells like engineering, infrastructure or a vendor.
const JARGON = [
  /[A-Z][A-Z0-9]*_[A-Z0-9_]{2,}/, // SCREAMING_CODES, ORACLE_FEATURE_X
  /\b(tenant|provider|deploy(?:ed|ment)?|backend|endpoint|env(?:ironment)? var|feature flag|migration|schema|payload|webhook|stack ?trace|traceback|exception|errno|null|undefined|NaN|json|sql|asyncpg|psycopg|uvicorn|fastapi|http|status code|request failed|aborted)\b/i,
  /\b(twilio|plivo|telnyx|sendgrid|mailgun|stripe|aws|azure|bedrock|fireworks|litellm|openai|gemini|regrid|attom|rentcast)\b/i,
  /[{}[\]<>`=]|::|\/api\//,
  // Bare HTTP reason phrases (res.statusText) are not explanations.
  /^(bad request|unauthorized|forbidden|not found|conflict|unprocessable (entity|content)|too many requests|internal server error|bad gateway|service unavailable|gateway timeout)\.?$/i,
];

function technicalText(error) {
  if (!error) return '';
  if (typeof error === 'string') return error;
  const parts = [];
  if (error.status) parts.push(String(error.status));
  if (error.code) parts.push(String(error.code));
  if (error.message) parts.push(String(error.message));
  return parts.join(' · ');
}

/** True when a server message already reads like a sentence for a person. */
export function isHumanMessage(message) {
  if (typeof message !== 'string') return false;
  const text = message.trim();
  if (text.length < 8 || text.length > 200) return false;
  if (!/\s/.test(text)) return false; // single tokens are codes
  if (!/^[A-Z0-9"'“(]/.test(text)) return false; // sentences start capitalised
  return !JARGON.some((pattern) => pattern.test(text));
}

function isUnavailable(error) {
  if (!error) return false;
  if (error.isUnavailable === true) return true;
  const status = Number(error.status) || 0;
  const origin = Number(error.originStatus) || 0;
  return status === 502 || status === 503 || status === 504 || origin === 503;
}

function isTimeout(error) {
  return error?.status === 408 || (error?.status === 0 && /timed out/i.test(error?.message || ''));
}

function isAbort(error) {
  return error?.name === 'AbortError' || (error?.status === 0 && /aborted/i.test(error?.message || ''));
}

function baseSentence(error, { capability, fallback } = {}) {
  if (!error) return fallback || ERROR_MESSAGES.DEFAULT;
  if (error.isNetworkError || (typeof navigator !== 'undefined' && navigator.onLine === false)) {
    return ERROR_MESSAGES.NETWORK_ERROR;
  }
  if (isTimeout(error)) return ERROR_MESSAGES.TIMEOUT;
  const status = Number(error.status) || 0;
  // A capability that is switched off or down is a degraded state, not a crash.
  if (capability && status >= 500) {
    return `${capability} is temporarily unavailable. The rest of Neoh still works.`;
  }
  if (isUnavailable(error)) {
    // A JSON 503 straight from the backend (dev, or an edge that passes it
    // through) carries a product sentence worth showing; an edge page never does.
    if (status === 503 && isHumanMessage(error.message)) return error.message.trim();
    return ERROR_MESSAGES.UNAVAILABLE;
  }
  // Validation-ish answers are often the most useful thing the server said.
  if ((status === 400 || status === 409 || status === 422) && isHumanMessage(error.message)) {
    return error.message.trim();
  }
  if (STATUS_MESSAGES[status]) return STATUS_MESSAGES[status];
  // A plain Error thrown by our own code (no status) with a human sentence is
  // fine to show as-is.
  if (!status && isHumanMessage(error.message)) return error.message.trim();
  // 5xx and anything unrecognised: the call site's own sentence says more
  // than a generic one.
  if (fallback) return fallback;
  return status >= 500 ? ERROR_MESSAGES.SERVER_ERROR : ERROR_MESSAGES.DEFAULT;
}

/**
 * Turn any thrown value into one sentence a customer can act on.
 *
 * @param {unknown} error   ApiError, Error, string, or anything thrown.
 * @param {{ action?: string, capability?: string, fallback?: string }} [options]
 *   action     — what the user was doing, lowercase verb phrase:
 *                "load your contacts" → "Couldn't load your contacts. <why>"
 *   capability — customer name for the feature ("Calling", "Messaging", "MLS
 *                search") used for 5xx/503 degraded wording.
 *   fallback   — the call site's own sentence ("Listings could not be loaded."),
 *                used for 5xx / unrecognised failures instead of a generic one.
 *                Network, auth, permission, not-found, conflict, rate-limit and
 *                human-readable validation answers still take precedence.
 */
export function friendlyError(error, options = {}) {
  const { action } = options;
  if (isAbort(error) && !isTimeout(error)) return '';
  const sentence = baseSentence(error, options);
  if (!action) return sentence;
  // "Couldn't load your contacts. Check your connection and try again."
  const why = sentence
    .replace(/^Couldn't reach Neoh\. /, "Neoh couldn't be reached. ")
    .replace(/^Something went wrong\. /, '');
  return `Couldn't ${action}. ${why}`.trim();
}

/** Same as friendlyError, plus whether a retry button makes sense and the raw detail for logs. */
export function describeError(error, options = {}) {
  return {
    message: friendlyError(error, options),
    retryable: isRetryableError(error) || isTimeout(error),
    technical: technicalText(error),
  };
}

/** Back-compat name used by older call sites; same product language. */
export function formatApiError(error) {
  return friendlyError(error);
}

export function isAuthError(error) {
  return error?.status === 401;
}

export function isNetworkError(error) {
  return error?.isNetworkError === true;
}

export function isRetryableError(error) {
  if (error?.isNetworkError) return true;
  const status = error?.status;
  if (!status) return false;
  if (status === 408 || status === 429) return true;
  if (status >= 500 && status < 600) return true;
  return false;
}
