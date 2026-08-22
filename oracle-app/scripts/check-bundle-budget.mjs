#!/usr/bin/env node
/**
 * Fail the build when the JS bundle grows past its checked-in budget.
 *
 * Bundle weight only ever regresses by accident — nobody sets out to add
 * 400 KB, they add a panel that statically imports a viewer that pulls in a
 * WebGL engine. Vite's own `chunkSizeWarningLimit` prints a warning and exits
 * 0, so in CI it scrolls past. This exits non-zero.
 *
 * The budget is a ratchet, not a target: lower it deliberately when a phase
 * makes things smaller. Raising it should be a reviewed decision with a reason
 * in the commit message, never a reflex to make a red build green.
 *
 *   node scripts/check-bundle-budget.mjs           # check against the budget
 *   node scripts/check-bundle-budget.mjs --update  # rewrite the budget to current
 */

import { readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const assetsDir = join(root, 'dist', 'assets');
const budgetPath = join(root, 'bundle-budget.json');

if (!existsSync(assetsDir)) {
  console.error(`No build output at ${assetsDir} — run \`npm run build\` first.`);
  process.exit(2);
}

const chunks = readdirSync(assetsDir)
  .filter((name) => name.endsWith('.js'))
  .map((name) => {
    const bytes = readFileSync(join(assetsDir, name));
    return { name, raw: bytes.length, gzip: gzipSync(bytes, { level: 9 }).length };
  })
  .sort((a, b) => b.raw - a.raw);

if (chunks.length === 0) {
  console.error('Build produced no JS chunks — that is not a passing state.');
  process.exit(2);
}

const actual = {
  largestChunkBytes: chunks[0].raw,
  largestChunkGzipBytes: chunks[0].gzip,
  totalBytes: chunks.reduce((sum, c) => sum + c.raw, 0),
  totalGzipBytes: chunks.reduce((sum, c) => sum + c.gzip, 0),
};

const kb = (n) => `${(n / 1024).toFixed(1)} KB`;

if (process.argv.includes('--update')) {
  writeFileSync(
    budgetPath,
    JSON.stringify(
      {
        '// note': 'Ceilings, not targets. Lower deliberately; raise only with a stated reason.',
        '// largest': chunks[0].name,
        ...actual,
      },
      null,
      2,
    ) + '\n',
  );
  console.log(`Budget written to bundle-budget.json (largest: ${chunks[0].name}, ${kb(actual.largestChunkBytes)}).`);
  process.exit(0);
}

if (!existsSync(budgetPath)) {
  console.error(`No bundle-budget.json — seed it with: node scripts/check-bundle-budget.mjs --update`);
  process.exit(2);
}

const budget = JSON.parse(readFileSync(budgetPath, 'utf8'));
const limits = [
  ['largestChunkBytes', 'largest chunk (raw)'],
  ['largestChunkGzipBytes', 'largest chunk (gzip)'],
  ['totalBytes', 'total JS (raw)'],
  ['totalGzipBytes', 'total JS (gzip)'],
];

const failures = [];
for (const [key, label] of limits) {
  const limit = budget[key];
  if (typeof limit !== 'number') continue;
  if (actual[key] > limit) {
    failures.push(`  ${label}: ${kb(actual[key])} exceeds budget ${kb(limit)} (+${kb(actual[key] - limit)})`);
  }
}

console.log(`Largest chunk: ${chunks[0].name} — ${kb(actual.largestChunkBytes)} raw, ${kb(actual.largestChunkGzipBytes)} gzip`);
console.log(`Total JS: ${kb(actual.totalBytes)} raw, ${kb(actual.totalGzipBytes)} gzip across ${chunks.length} chunks`);

if (failures.length > 0) {
  console.error('\nBundle budget exceeded:\n' + failures.join('\n'));
  console.error(
    '\nSplit the offending code, or lazy-load it. If the growth is genuinely ' +
    'warranted, run `node scripts/check-bundle-budget.mjs --update` and say why in the commit.',
  );
  process.exit(1);
}

console.log('\nWithin budget.');
