import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

// The service worker cannot load in jsdom (workbox, `self`), so this pins its
// source: a cached splat or legal payload leaves IndexedDB only through
// serveSplat, which asks the server first (lib/splatCacheGate.js, T16.14).
// A postMessage that hands back the cached bytes would skip that check, so a
// viewer whose access was removed could still read them (T16.06, L16-05).
const source = readFileSync(new URL('./sw-oracle.js', import.meta.url), 'utf8');

describe('the service worker serves cached media only through the rights check', () => {
  it('has no message that hands back cached bytes or payloads', () => {
    expect(source).not.toMatch(/CACHE_RETRIEVE_SPLAT|CACHE_RETRIEVE_LEGAL/);
    expect(source).not.toMatch(/blob:\s*entry\.blob/);
    expect(source).not.toMatch(/payload:\s*entry\.payload/);
  });

  it('reads the splat store only for the gate, presence and prefetch dedupe', () => {
    const reads = source.match(/idbGet\(STORE_SPLATS/g) || [];
    expect(reads.length).toBeLessThanOrEqual(3);
    expect(source).toMatch(/serveCachedSplat\(request,\s*\{\s*get: \(id\) => idbGet\(STORE_SPLATS, id\)/);
  });
});

describe('the service worker never holds the cache database open (WEB-001)', () => {
  it('yields to a delete and closes after each transaction', () => {
    expect(source).toMatch(/db\.onversionchange = \(\) => db\.close\(\)/);
    for (const fn of ['idbPut', 'idbGet', 'idbDelete']) {
      const body = source.slice(source.indexOf(`async function ${fn}(`), source.indexOf(`async function ${fn}(`) + 600);
      expect(body, fn).toMatch(/db\.close\(\)/);
    }
  });

  it('does not treat a blocked deletion as done', () => {
    const clear = source.slice(source.indexOf("type === 'CLEAR_PRIVATE_CACHE'"), source.indexOf("type === 'CLEAR_PRIVATE_CACHE'") + 900);
    expect(clear).not.toMatch(/onblocked = \(\) => resolve\(\)|req\.onblocked = \(\) => resolve/);
    expect(clear).not.toMatch(/req\.onsuccess = req\.onerror = req\.onblocked/);
  });
});
