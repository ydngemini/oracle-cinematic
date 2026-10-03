import { jitteredBackoff, retryAfterMs } from './backoff.js';
const configuredApiBase = import.meta.env.VITE_API_BASE || '';
// Same origin by default, in dev too: Vite proxies /api, /auth and /ws to the
// backend. The old dev fallback to an absolute http://localhost:8000 assumed
// the backend's port was reachable from the browser's host, which under
// Docker-in-Docker it is not — and because it was a fallback, setting
// VITE_API_BASE empty did not disable it.
const API_BASE = configuredApiBase.replace(/\/+$/, '');

const DEFAULT_TIMEOUT = 30000;
const MAX_RETRIES = 3;
const RETRYABLE_STATUS_CODES = new Set([408, 429]);
// Longest server-requested wait worth sleeping through inside one request.
const MAX_RETRY_AFTER_MS = 10000;

export class ApiError extends Error {
  constructor(detail, status, isNetworkError = false) {
    const message = typeof detail === 'string'
      ? detail
      : detail?.message || detail?.detail || 'Request failed';
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.isNetworkError = isNetworkError;
    this.detail = detail;
    this.code = typeof detail === 'object' ? detail?.code || '' : '';
    this.timestamp = new Date().toISOString();
  }
}

function isRetryable(error) {
  if (error.isNetworkError) return true;
  const status = error.status;
  if (RETRYABLE_STATUS_CODES.has(status)) return true;
  if (status >= 500 && status < 600) return true;
  return false;
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function authHeaders(tokenOverride) {
  return tokenOverride ? { Authorization: `Bearer ${tokenOverride}` } : {};
}

let csrfToken = '';
let csrfPromise = null;

function resetCsrfToken() {
  csrfToken = '';
  csrfPromise = null;
}

async function getCsrfToken() {
  if (csrfToken) return csrfToken;
  if (!csrfPromise) {
    csrfPromise = fetch(`${API_BASE}/auth/csrf`, { credentials: 'include', cache: 'no-store' })
      .then(async (res) => {
        if (!res.ok) throw new ApiError('Unable to initialize request security.', res.status, false);
        const payload = await readJson(res);
        if (!payload?.csrf_token) throw new ApiError('Invalid request-security response.', 0, false);
        csrfToken = payload.csrf_token;
        return csrfToken;
      })
      .finally(() => { csrfPromise = null; });
  }
  return csrfPromise;
}

// Production sits behind DigitalOcean App Platform, whose edge REPLACES a
// backend 503 with its own HTML 504 page (`content-type: text/html`,
// `x-do-orig-status: 503`) and drops the JSON body and Retry-After. So: only
// ever parse a body that says it is JSON, never surface HTML, and treat
// 502/503/504 as "temporarily unavailable" whatever the body looked like.
export const UNAVAILABLE_STATUSES = new Set([502, 503, 504]);
// Default wait before retrying an unavailable response when the edge has
// stripped Retry-After (jittered, doubled per attempt, capped).
export const UNAVAILABLE_RETRY_BASE_MS = 2000;

function isJsonResponse(res) {
  return /\bjson\b/i.test(res.headers?.get?.('content-type') || '');
}

async function parseErrorResponse(res) {
  // statusText is empty on HTTP/2; never fall back to the body text.
  let detail = res.statusText || '';
  if (!isJsonResponse(res)) return detail;
  try {
    const data = await res.json();
    if (data?.code) {
      return {
        message: data.detail || data.message || detail,
        code: data.code,
      };
    }
    detail = data.detail || data.message || detail;
  } catch {
    // Mislabelled or truncated JSON — keep the status, drop the body.
  }
  return detail;
}

/** ApiError for a non-OK response, annotated for the error-language layer. */
export async function errorFromResponse(res) {
  const error = new ApiError(await parseErrorResponse(res), res.status, false);
  error.contentType = res.headers?.get?.('content-type') || '';
  // Readable only if CORS exposes it; nothing depends on it being present.
  const orig = Number(res.headers?.get?.('x-do-orig-status'));
  error.originStatus = Number.isFinite(orig) && orig > 0 ? orig : null;
  error.isUnavailable = UNAVAILABLE_STATUSES.has(res.status) || UNAVAILABLE_STATUSES.has(error.originStatus);
  return error;
}

/** res.json() that never leaks "Unexpected token <" from an HTML page. */
async function readJson(res) {
  try {
    return await res.json();
  } catch {
    const error = new ApiError('Unexpected response', 502, false);
    error.contentType = res.headers?.get?.('content-type') || '';
    error.isUnavailable = true;
    throw error;
  }
}

function unavailableWait(res, attempt) {
  const serverWait = retryAfterMs(res.headers?.get?.('retry-after'));
  return serverWait ?? jitteredBackoff(attempt, { base: UNAVAILABLE_RETRY_BASE_MS, max: 30000 });
}

export async function fetchWithRetry(path, options = {}) {
  const {
    method = 'GET',
    body,
    token,
    timeout = DEFAULT_TIMEOUT,
    retries = MAX_RETRIES,
    retryUnsafe = false,
    signal,
  } = options;

  const url = `${API_BASE}${path}`;
  const normalizedMethod = method.toUpperCase();
  const allowedRetries = ['GET', 'HEAD', 'OPTIONS'].includes(normalizedMethod) || retryUnsafe
    ? retries
    : 0;
  const headers = {
    ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    ...authHeaders(token),
  };
  if (!['GET', 'HEAD', 'OPTIONS'].includes(normalizedMethod)) {
    headers['X-CSRF-Token'] = await getCsrfToken();
  }

  let lastError;

  let attempt = 0;
  let csrfRetried = false;
  while (attempt <= allowedRetries) {
    if (signal?.aborted) {
      throw new ApiError('Request aborted', 0, false);
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);
    const combinedSignal = signal
      ? AbortSignal.any ? AbortSignal.any([signal, controller.signal]) : controller.signal
      : controller.signal;

    try {
      const res = await fetch(url, {
        method,
        headers,
        credentials: 'include',
        signal: combinedSignal,
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });

      clearTimeout(timeoutId);

      if (!res.ok) {
        const error = await errorFromResponse(res);

        // The CSRF middleware rejects before dispatching the route, so exactly
        // one token refresh + replay is safe even for an otherwise non-retryable
        // mutation. This also heals logout and cross-tab token changes.
        if (
          res.status === 403
          && error.code === 'CSRF_TOKEN_INVALID'
          && !csrfRetried
          && !['GET', 'HEAD', 'OPTIONS'].includes(normalizedMethod)
        ) {
          resetCsrfToken();
          headers['X-CSRF-Token'] = await getCsrfToken();
          csrfRetried = true;
          continue;
        }

        if (res.status === 401) {
          window.dispatchEvent(new CustomEvent('auth:expired', { detail: error }));
        }

        if (isRetryable(error) && attempt < allowedRetries) {
          // A 429 says when to come back. Retrying sooner cannot succeed and
          // counts against the same window again; a wait longer than a user
          // will sit through is surfaced instead of slept on.
          const serverWait = res.status === 429 ? retryAfterMs(res.headers.get('retry-after')) : null;
          if (serverWait != null && serverWait > MAX_RETRY_AFTER_MS) throw error;
          const backoff = serverWait
            ?? (error.isUnavailable ? unavailableWait(res, attempt) : jitteredBackoff(attempt, { base: 1000, max: 30000 }));
          if (backoff > MAX_RETRY_AFTER_MS) throw error;
          await delay(backoff);
          lastError = error;
          attempt += 1;
          continue;
        }

        throw error;
      }

      if (res.status === 204) return null;
      return readJson(res);

    } catch (err) {
      clearTimeout(timeoutId);

      if (err instanceof ApiError) {
        throw err;
      }

      if (err.name === 'AbortError') {
        const isTimeout = !signal?.aborted;
        throw new ApiError(
          isTimeout ? 'Request timed out' : 'Request aborted',
          0,
          false
        );
      }

      const networkError = new ApiError(
        'Network error - please check your connection',
        0,
        true
      );

      if (isRetryable(networkError) && attempt < allowedRetries) {
        const backoff = jitteredBackoff(attempt, { base: 1000, max: 30000 });
        await delay(backoff);
        lastError = networkError;
        attempt += 1;
        continue;
      }

      throw networkError;
    }
  }

  throw lastError;
}

/**
 * POST a JSON body and save what comes back: either a short-lived signed URL
 * (`{url}`, object-storage backends) or the file itself. Used where a download
 * must carry a re-entered password, which a plain GET link cannot.
 */
export async function postForDownload(path, body, filename, options = {}) {
  const { token, timeout = 120000 } = options;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(token),
        'X-CSRF-Token': await getCsrfToken(),
      },
      body: JSON.stringify(body),
      credentials: 'include',
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!res.ok) {
      throw await errorFromResponse(res);
    }
    const link = window.document.createElement('a');
    link.style.display = 'none';
    link.rel = 'noopener';
    if ((res.headers.get('content-type') || '').includes('application/json')) {
      const data = await res.json();
      if (!data?.url) throw new ApiError('Download unavailable', 503, false);
      link.href = data.url;
    } else {
      const buffer = await res.arrayBuffer();
      link.href = URL.createObjectURL(new Blob([buffer], { type: 'application/zip' }));
      link.download = filename;
    }
    window.document.body.append(link);
    link.click();
    link.remove();
    if (link.href.startsWith('blob:')) setTimeout(() => URL.revokeObjectURL(link.href), 0);
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function fetchBlob(path, options = {}) {
  const { token, timeout = DEFAULT_TIMEOUT, signal } = options;
  const url = `${API_BASE}${path}`;
  const headers = authHeaders(token);

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  const combinedSignal = signal
    ? AbortSignal.any ? AbortSignal.any([signal, controller.signal]) : controller.signal
    : controller.signal;

  try {
    const res = await fetch(url, {
      headers,
      credentials: 'include',
      cache: 'no-store',
      signal: combinedSignal,
    });

    clearTimeout(timeoutId);

    if (!res.ok) {
      throw await errorFromResponse(res);
    }

    // Materialise the bytes ourselves rather than calling res.blob().
    // Chrome's own response-to-Blob path fails outright on a large body here —
    // a 13 MB Gaussian-splat capture threw "TypeError: Failed to fetch" while
    // reading the very same response as a stream or an ArrayBuffer returned
    // all 13,262,546 bytes. A 3D tour is the one thing in this product that is
    // routinely that big, and it simply would not open.
    const buffer = await res.arrayBuffer();
    return new Blob([buffer], {
      type: res.headers.get('content-type') || 'application/octet-stream',
    });
  } catch (err) {
    clearTimeout(timeoutId);

    if (err instanceof ApiError) throw err;

    if (err.name === 'AbortError') {
      const isTimeout = !signal?.aborted;
      throw new ApiError(
        isTimeout ? 'Request timed out' : 'Request aborted',
        0,
        false
      );
    }

    throw new ApiError('Network error - please check your connection', 0, true);
  }
}

export async function uploadFile(path, formData, options = {}) {
  const { token, timeout = DEFAULT_TIMEOUT, signal } = options;
  const url = `${API_BASE}${path}`;
  const headers = authHeaders(token);
  headers['X-CSRF-Token'] = await getCsrfToken();

  for (let csrfAttempt = 0; csrfAttempt < 2; csrfAttempt += 1) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);
    const combinedSignal = signal
      ? AbortSignal.any ? AbortSignal.any([signal, controller.signal]) : controller.signal
      : controller.signal;

    try {
      const res = await fetch(url, {
        method: 'POST',
        headers,
        credentials: 'include',
        body: formData,
        signal: combinedSignal,
      });

      clearTimeout(timeoutId);

      if (!res.ok) {
        const error = await errorFromResponse(res);
        if (
          res.status === 403
          && error.code === 'CSRF_TOKEN_INVALID'
          && csrfAttempt === 0
        ) {
          resetCsrfToken();
          headers['X-CSRF-Token'] = await getCsrfToken();
          continue;
        }
        throw error;
      }

      return readJson(res);
    } catch (err) {
      clearTimeout(timeoutId);

      if (err instanceof ApiError) throw err;

      if (err.name === 'AbortError') {
        const isTimeout = !signal?.aborted;
        throw new ApiError(
          isTimeout ? 'Request timed out' : 'Request aborted',
          0,
          false
        );
      }

      throw new ApiError('Network error - please check your connection', 0, true);
    }
  }

  throw new ApiError('Unable to refresh request security.', 403, false);
}

export const apiGet = (path, options) => fetchWithRetry(path, { ...options, method: 'GET' });
export const apiPost = (path, body, options) => fetchWithRetry(path, { ...options, method: 'POST', body });
export const apiPut = (path, body, options) => fetchWithRetry(path, { ...options, method: 'PUT', body });
export const apiPatch = (path, body, options) => fetchWithRetry(path, { ...options, method: 'PATCH', body });
export const apiDelete = (path, options) => fetchWithRetry(path, { ...options, method: 'DELETE' });
