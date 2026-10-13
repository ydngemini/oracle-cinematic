// @vitest-environment node
/**
 * Prompt 02 AC02.01: the all-pages matrix covers every registry entry. Fails
 * on a page a registry declares but the matrix lacks, on a stale matrix row,
 * and on a page whose component cannot be found. Regenerate with
 * `npm run pages:matrix`.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { buildMatrix } from '../scripts/build-all-pages-matrix.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const committed = JSON.parse(readFileSync(join(HERE, '../../docs/claude-prompts/all-pages.json'), 'utf8')).pages;
// API lists are refreshed on regeneration, not gated here.
const strip = (row) => { const rest = { ...row }; delete rest.apis; return rest; };

describe('ALL-PAGES-MATRIX (prompt 02 AC02.01)', () => {
  it('has a row for every page the registries declare, and no stale rows', async () => {
    const built = await buildMatrix();
    const want = built.map((r) => r.id);
    const have = committed.map((r) => r.id);
    expect(want.filter((id) => !have.includes(id)), 'pages missing from the matrix: run npm run pages:matrix').toEqual([]);
    expect(have.filter((id) => !want.includes(id)), 'stale matrix rows: run npm run pages:matrix').toEqual([]);
  });

  it('matches the registries row by row (paths, components, roles, verdicts)', async () => {
    const built = await buildMatrix();
    expect(committed.map(strip)).toEqual(built.map(strip));
  });

  it('resolves every page to a component file', async () => {
    const built = await buildMatrix();
    expect(built.filter((r) => !r.component_file).map((r) => r.id)).toEqual([]);
  });

  it('offers no MLS page (D-2026-10-12-01, prompt 11 T11.01)', async () => {
    const built = await buildMatrix();
    expect(built.filter((r) => /mls/i.test(r.id) || r.legacy).map((r) => r.id)).toEqual([]);
    expect(built.flatMap((r) => r.apis).filter((a) => a.includes('/api/mls/'))).toEqual([]);
  });
});
