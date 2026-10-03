import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, fetchWithRetry, UNAVAILABLE_RETRY_BASE_MS } from './apiClient';
import { ERROR_MESSAGES, friendlyError } from './errorMessages';

// What DigitalOcean App Platform's edge actually returns in place of a backend
// 503 (verified on staging): an HTML 504 page, the origin status in a header,
// no JSON body, no Retry-After.
const EDGE_504_HTML = '<!DOCTYPE html><html><head><title>504 Gateway Timeout</title></head><body>upstream</body></html>';

function respond(body, status, headers = {}) {
  return new Response(body, { status, statusText: '', headers });
}

async function failure(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error('expected the request to fail');
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('apiClient — 5xx bodies that are not JSON', () => {
  it('JSON 503 from the backend keeps its product sentence', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(
      JSON.stringify({ detail: 'Messaging isn’t set up for your brokerage yet.' }),
      503,
      { 'content-type': 'application/json' },
    )));
    const error = await failure(fetchWithRetry('/api/x', { retries: 0 }));
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(503);
    expect(error.isUnavailable).toBe(true);
    expect(friendlyError(error)).toBe('Messaging isn’t set up for your brokerage yet.');
  });

  it('JSON 503 with a developer detail reads as temporarily unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(
      JSON.stringify({ detail: 'asyncpg pool exhausted' }),
      503,
      { 'content-type': 'application/json' },
    )));
    const error = await failure(fetchWithRetry('/api/x', { retries: 0 }));
    expect(friendlyError(error)).toBe(ERROR_MESSAGES.UNAVAILABLE);
  });

  it('HTML 504 from the edge (x-do-orig-status: 503) never surfaces HTML', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(EDGE_504_HTML, 504, {
      'content-type': 'text/html',
      'x-do-orig-status': '503',
    })));
    const error = await failure(fetchWithRetry('/api/x', { retries: 0 }));
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(504);
    expect(error.originStatus).toBe(503);
    expect(error.isUnavailable).toBe(true);
    expect(error.message).not.toMatch(/<|DOCTYPE|Unexpected token/);
    expect(friendlyError(error)).toBe(ERROR_MESSAGES.UNAVAILABLE);
    expect(friendlyError(error, { fallback: 'Listings could not be loaded.' })).toBe(ERROR_MESSAGES.UNAVAILABLE);
    expect(friendlyError(error, { capability: 'Calling' })).toBe('Calling is temporarily unavailable. The rest of Neoh still works.');
  });

  it('HTML 504 without the origin header (CORS hid it) is still unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(EDGE_504_HTML, 504, { 'content-type': 'text/html' })));
    const error = await failure(fetchWithRetry('/api/x', { retries: 0 }));
    expect(error.originStatus).toBeNull();
    expect(friendlyError(error)).toBe(ERROR_MESSAGES.UNAVAILABLE);
  });

  it('HTML 502 reads as temporarily unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond('<html><body>Bad gateway</body></html>', 502, { 'content-type': 'text/html' })));
    const error = await failure(fetchWithRetry('/api/x', { retries: 0 }));
    expect(error.status).toBe(502);
    expect(error.message).not.toMatch(/</);
    expect(friendlyError(error)).toBe(ERROR_MESSAGES.UNAVAILABLE);
  });

  it('an HTML page on a 200 becomes an ApiError, not "Unexpected token <"', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond('<!doctype html><html></html>', 200, { 'content-type': 'text/html' })));
    const error = await failure(fetchWithRetry('/api/x', { retries: 0 }));
    expect(error).toBeInstanceOf(ApiError);
    expect(friendlyError(error)).toBe(ERROR_MESSAGES.UNAVAILABLE);
  });

  it('network failure is a connection hint', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    const error = await failure(fetchWithRetry('/api/x', { retries: 0 }));
    expect(error.isNetworkError).toBe(true);
    expect(friendlyError(error)).toBe(ERROR_MESSAGES.NETWORK_ERROR);
  });

  it('retries an edge 504 for a GET after the default wait, since Retry-After is gone', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(respond(EDGE_504_HTML, 504, { 'content-type': 'text/html', 'x-do-orig-status': '503' }))
      .mockResolvedValueOnce(respond(JSON.stringify({ ok: true }), 200, { 'content-type': 'application/json' }));
    vi.stubGlobal('fetch', fetchMock);
    const pending = fetchWithRetry('/api/x', { retries: 1 });
    await vi.advanceTimersByTimeAsync(UNAVAILABLE_RETRY_BASE_MS / 2 - 1);
    expect(fetchMock).toHaveBeenCalledTimes(1); // never sooner than half the base wait
    await vi.advanceTimersByTimeAsync(UNAVAILABLE_RETRY_BASE_MS);
    await expect(pending).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
