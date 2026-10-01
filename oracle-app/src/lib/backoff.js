// Retry timing shared by the WebSocket reconnect and the HTTP client.
//
// Jitter is the point. Without it every client that lost its connection at the
// same moment — an API replica restarting during a deploy drops all of its
// sockets at once — retries at exactly 2 s, 4 s, 8 s … in lockstep, so each
// wave arrives as one burst (Mission 8 capacity audit, §25).
//
// "Equal jitter": half the exponential step is fixed, half is random. The
// fixed half keeps a floor, so a client never hammers at ~0 ms; the random half
// spreads a crowd across the window.

export function jitteredBackoff(attempt, { base, max, random = Math.random }) {
  const cap = Math.min(max, base * 2 ** Math.max(0, attempt));
  return Math.round(cap / 2 + random() * (cap / 2));
}

// Retry-After as milliseconds (delta-seconds or an HTTP date), or null.
export function retryAfterMs(value, now = Date.now()) {
  if (value == null || value === '') return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : Math.max(0, at - now);
}
