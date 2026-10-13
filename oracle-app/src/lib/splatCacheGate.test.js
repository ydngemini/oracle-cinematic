import { describe, expect, it, vi } from 'vitest';
import { serveCachedSplat } from './splatCacheGate';

const URL_ = 'https://app.test/api/media/abc-123';
const req = () => new Request(URL_);

function deps({ cached = { blob: new Blob(['splat']) }, answer }) {
  return {
    get: vi.fn().mockResolvedValue(cached),
    remove: vi.fn().mockResolvedValue(undefined),
    fetch: vi.fn(typeof answer === 'function' ? answer : () => Promise.resolve(answer)),
  };
}

describe('serveCachedSplat (T16.14)', () => {
  it('serves the cached copy only after the server confirms access (304)', async () => {
    const d = deps({ answer: new Response(null, { status: 304 }) });
    const res = await serveCachedSplat(req(), d);
    expect(res.headers.get('X-Neoh-Cache')).toBe('predictive-hit');
    expect(await res.text()).toBe('splat');
    const [, init] = d.fetch.mock.calls[0];
    expect(init.headers['If-None-Match']).toBe('"abc-123"');
    expect(init.cache).toBe('no-store');
  });

  it.each([401, 403, 404, 410])('purges the copy and refuses when the server says %i', async (code) => {
    const d = deps({ answer: new Response('nope', { status: code }) });
    const res = await serveCachedSplat(req(), d);
    expect(res.status).toBe(code);
    expect(d.remove).toHaveBeenCalledWith('abc-123');
  });

  it('still works offline from the prefetched copy', async () => {
    const d = deps({ answer: () => Promise.reject(new TypeError('offline')) });
    const res = await serveCachedSplat(req(), d);
    expect(res.headers.get('X-Neoh-Cache')).toBe('predictive-hit-offline');
    expect(d.remove).not.toHaveBeenCalled();
  });

  it('does not mask a server error with a cached copy', async () => {
    const d = deps({ answer: new Response('down', { status: 503 }) });
    expect((await serveCachedSplat(req(), d)).status).toBe(503);
    expect(d.remove).not.toHaveBeenCalled();
  });

  it('goes to the network when nothing is cached', async () => {
    const d = deps({ cached: null, answer: new Response('bytes', { status: 200 }) });
    expect((await serveCachedSplat(req(), d)).status).toBe(200);
    expect(d.fetch.mock.calls[0][0]).toBeInstanceOf(Request);
  });
});
