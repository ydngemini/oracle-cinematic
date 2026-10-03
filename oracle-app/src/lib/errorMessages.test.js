import { describe, expect, it } from 'vitest';
import { ApiError } from './apiClient';
import { describeError, friendlyError, isHumanMessage, ERROR_MESSAGES } from './errorMessages';

const api = (message, status, isNetwork = false) => new ApiError(message, status, isNetwork);

describe('friendlyError — one product-language path', () => {
  it('turns a network failure into a connection hint, never "backend unreachable"', () => {
    const text = friendlyError(api('Network error - please check your connection', 0, true));
    expect(text).toBe(ERROR_MESSAGES.NETWORK_ERROR);
    expect(text).not.toMatch(/backend/i);
  });

  it('hides provider codes and env flags behind the call site sentence', () => {
    const plivo = api('PLIVO_API_ERROR 500', 500);
    expect(friendlyError(plivo, { fallback: 'Calling is down.' })).toBe('Calling is down.');
    expect(friendlyError(api('Set ORACLE_FEATURE_VIDEO_STUDIO and restart', 500))).toBe(ERROR_MESSAGES.SERVER_ERROR);
    // 502/503/504 are "temporarily unavailable" whatever the call site said.
    expect(friendlyError(api('PLIVO_API_ERROR 503', 503), { fallback: 'Calling is down.' })).toBe(ERROR_MESSAGES.UNAVAILABLE);
  });

  it('names a degraded capability and reassures the rest still works', () => {
    expect(friendlyError(api('twilio down', 503), { capability: 'Calling' }))
      .toBe('Calling is temporarily unavailable. The rest of Neoh still works.');
  });

  it('passes a human validation sentence through', () => {
    expect(friendlyError(api('That email is already invited.', 409))).toBe('That email is already invited.');
  });

  it('does not pass developer-facing validation text through', () => {
    expect(friendlyError(api('client_id must be a UUID', 422))).toBe(ERROR_MESSAGES.VALIDATION_ERROR);
    expect(friendlyError(api('Tenant boundary violation', 403))).toBe(ERROR_MESSAGES.FORBIDDEN);
  });

  it('maps auth, permission, missing and rate limit to plain sentences', () => {
    expect(friendlyError(api('x', 401))).toBe(ERROR_MESSAGES.UNAUTHORIZED);
    expect(friendlyError(api('Forbidden', 403))).toBe(ERROR_MESSAGES.FORBIDDEN);
    expect(friendlyError(api('Not Found', 404))).toBe(ERROR_MESSAGES.NOT_FOUND);
    expect(friendlyError(api('Too Many Requests', 429))).toBe(ERROR_MESSAGES.RATE_LIMITED);
    expect(friendlyError(api('Request timed out', 0))).toBe(ERROR_MESSAGES.TIMEOUT);
  });

  it('prefixes the action when given', () => {
    expect(friendlyError(api('boom', 500), { action: 'load your contacts' }))
      .toBe("Couldn't load your contacts. Neoh couldn't finish that just now. Try again in a moment.");
  });

  it('stays quiet for a user-cancelled request', () => {
    expect(friendlyError(api('Request aborted', 0))).toBe('');
  });

  it('handles nothing and plain strings', () => {
    expect(friendlyError(null)).toBe(ERROR_MESSAGES.DEFAULT);
    expect(friendlyError(null, { fallback: 'Listings could not be loaded.' })).toBe('Listings could not be loaded.');
    expect(friendlyError(new Error('Choose a state first.'))).toBe('Choose a state first.');
  });

  it('keeps the raw detail for logs and says when a retry helps', () => {
    const detail = describeError(api('PLIVO_API_ERROR', 500), { fallback: 'Calling is down.' });
    expect(detail).toEqual({ message: 'Calling is down.', retryable: true, technical: '500 · PLIVO_API_ERROR' });
    expect(describeError(api('Not Found', 404)).retryable).toBe(false);
  });

  it('ApiError no longer promotes a bare code to its message', () => {
    expect(new ApiError({ code: 'PLIVO_API_ERROR' }, 503).message).toBe('Request failed');
  });
});

describe('isHumanMessage', () => {
  it.each([
    ['That email is already invited.', true],
    ['Choose an existing seller or enter a new one, not both.', true],
    ['client not found', false],
    ['PLIVO_API_ERROR', false],
    ['Twilio rejected the request', false],
    ['Not enabled on this deployment', false],
    ['{"detail": "x"}', false],
  ])('%s → %s', (text, expected) => {
    expect(isHumanMessage(text)).toBe(expected);
  });
});
