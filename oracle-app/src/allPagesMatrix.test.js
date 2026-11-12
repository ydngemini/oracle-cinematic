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
import { buildMatrix, verdictProblems, verdictQueue, verdictShapeProblems } from '../scripts/build-all-pages-matrix.mjs';

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

  it('only lets a pass rest on a clean crawl (AC02.09)', () => {
    const verdict = (v) => ({ verdict: v, evidence: 'e', reviewer: 'r', date: '2026-10-21' });
    const rows = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }, { id: 'e' }, { id: 'mls', legacy: 'out of scope' }];
    const crawl = {
      a: { status: 'clean', writes_attempted_on_open: [] },
      b: { status: 'not_offered', writes_attempted_on_open: [] },
      c: { status: 'clean', writes_attempted_on_open: ['POST /api/x'] },
      mls: { status: 'clean', writes_attempted_on_open: [] },
    };
    expect(verdictProblems({ a: verdict('pass') }, rows, crawl)).toEqual([]);
    expect(verdictProblems({ b: verdict('pass') }, rows, crawl)).toHaveLength(1);
    expect(verdictProblems({ c: verdict('pass') }, rows, crawl)).toHaveLength(1);
    expect(verdictProblems({ d: verdict('pass') }, rows, crawl)).toHaveLength(1);
    expect(verdictProblems({ mls: verdict('pass') }, rows, crawl)).toHaveLength(1);
    // fail and setup_required never need a clean crawl; comment keys are skipped.
    expect(verdictProblems({ '// how': 'x', b: verdict('fail'), d: verdict('setup_required') }, rows, crawl)).toEqual([]);
  });

  it('rejects malformed or self-signed verdicts (AC02.09)', () => {
    const ok = { verdict: 'fail', evidence: 'run 1', reviewer: 'Dana Lee', date: '2026-02-28' };
    expect(verdictShapeProblems({ '// how': 'x', a: ok })).toEqual([]);
    expect(verdictShapeProblems({ a: { ...ok, evidence: '   ' } })).toHaveLength(1);
    expect(verdictShapeProblems({ a: { ...ok, reviewer: ' ' } })).toHaveLength(1);
    expect(verdictShapeProblems({ a: 'pass', b: null, c: [] })).toHaveLength(3);
    expect(verdictShapeProblems({ a: { ...ok, verdict: 'maybe' } })).toHaveLength(1);
    expect(verdictShapeProblems({ a: { ...ok, date: '2026-02-31' } })).toHaveLength(1);
    expect(verdictShapeProblems({ a: { ...ok, date: '2026-13-01' } })).toHaveLength(1);
    expect(verdictShapeProblems({ a: { ...ok, evidence: 42 } })).toHaveLength(1);
    for (const who of ['claude', 'Claude Code', 'neoh-agent', 'auto', 'bot']) {
      expect(verdictShapeProblems({ a: { ...ok, reviewer: who } }), who).toHaveLength(1);
    }
    expect(verdictShapeProblems({ a: { ...ok, reviewer: 'Claudette Moreau' } })).toEqual([]);
  });

  it('builds a review queue and never marks a risky page ready (AC02.09)', () => {
    const none = 'not_inspected';
    const clean = { status: 'clean', writes_attempted_on_open: [] };
    const rows = [
      { id: 'ok', verdict: none, crawl: clean },
      { id: 'signed', verdict: 'pass', crawl: clean },
      { id: 'broken', verdict: none, crawl: { status: 'issues', writes_attempted_on_open: [] } },
      { id: 'writer', verdict: none, crawl: { status: 'clean', writes_attempted_on_open: ['POST /api/x'] } },
      { id: 'hidden', verdict: none, crawl: null },
      { id: 'mls', verdict: none, legacy: 'out of scope', crawl: clean },
      { id: 'failed', verdict: 'fail', crawl: null },
    ];
    expect(verdictQueue(rows)).toEqual({
      total: 7,
      recorded: ['signed', 'failed'],
      ready_for_review: ['ok'],
      needs_attention: ['broken (crawl issues)', 'writer (writes on open)'],
      not_crawled: ['hidden'],
      legacy: ['mls'],
    });
    expect(verdictQueue([]).total).toBe(0);
  });

  it('offers no MLS page (D-2026-10-12-01, prompt 11 T11.01)', async () => {
    const built = await buildMatrix();
    expect(built.filter((r) => /mls/i.test(r.id) || r.legacy).map((r) => r.id)).toEqual([]);
    expect(built.flatMap((r) => r.apis).filter((a) => a.includes('/api/mls/'))).toEqual([]);
  });
});
