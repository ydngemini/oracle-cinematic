import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { KNOWN_PRIMITIVES, REGISTRY } from './registry';

const here = dirname(fileURLToPath(import.meta.url));
const renderPy = resolve(here, '../../../backend/neoh_render.py');

describe('the vocabulary is closed and shared', () => {
  it('draws every primitive the backend is allowed to emit', () => {
    // The two halves are a contract across a language boundary, and the only
    // thing that keeps them honest is reading the other side. A primitive the
    // backend can build but this file cannot draw is a silently missing block.
    const source = readFileSync(renderPy, 'utf8');
    const tuple = source.split('PRIMITIVES = (')[1].split(')')[0];
    const backend = [...tuple.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);

    expect(backend.length).toBeGreaterThan(0);
    const undrawable = backend.filter((name) => !KNOWN_PRIMITIVES.includes(name));
    // approval/receipt/mission are declared for the missions work and are not
    // emitted by any intent yet; every primitive an intent CAN emit must draw.
    expect(undrawable.sort()).toEqual(['approval', 'mission', 'receipt']);
  });

  it('draws nothing the backend cannot name', () => {
    const source = readFileSync(renderPy, 'utf8');
    const tuple = source.split('PRIMITIVES = (')[1].split(')')[0];
    const backend = [...tuple.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
    for (const name of KNOWN_PRIMITIVES) {
      expect(backend, `${name} is drawn but the backend cannot emit it`).toContain(name);
    }
  });

  it('every entry is a function of (props, ctx)', () => {
    for (const [name, draw] of Object.entries(REGISTRY)) {
      expect(typeof draw, name).toBe('function');
      expect(draw.length, `${name} must accept props and ctx`).toBeLessThanOrEqual(2);
    }
  });
});
