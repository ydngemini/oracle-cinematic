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
