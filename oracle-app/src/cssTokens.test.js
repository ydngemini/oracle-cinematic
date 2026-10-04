import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * Every custom property a stylesheet reads must exist.
 *
 * Property View, the rehab editor and the state-document checklist read
 * `--text-primary`, `--surface-glass`, `--border-subtle`… — names this design
 * system never defined. Each had a dark-theme fallback, so on the light theme
 * the address lookup rendered near-white text (#f4f4f5) on paper and a
 * dark-grey search field: unreadable, and invisible to every check because a
 * fallback is not an error. A fallback is allowed as a safety net, never as
 * the only definition.
 */

const SRC = fileURLToPath(new URL('.', import.meta.url));

function walk(dir, ext, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, ext, out);
    else if (ext.some((e) => name.endsWith(e)) && !name.includes('.test.')) out.push(path);
  }
  return out;
}

// Deliberate override hooks, read with a fallback and set by no theme.
const RUNTIME_ONLY = new Set(['--oracle-scrim', '--slide-y-offset']);

describe('CSS custom properties', () => {
  it('reads no variable that neither a stylesheet nor the runtime defines', () => {
    const css = walk(SRC, ['.css']).map((path) => [path, readFileSync(path, 'utf8')]);
    const code = walk(SRC, ['.js', '.jsx', '.ts', '.tsx']).map((path) => readFileSync(path, 'utf8')).join('\n');
    const defined = new Set();
    for (const [, text] of css) for (const m of text.matchAll(/(--[a-z0-9-]+)\s*:/gi)) defined.add(m[1]);
    for (const m of code.matchAll(/['"`](--[a-z0-9-]+)['"`]/gi)) defined.add(m[1]);
    for (const m of code.matchAll(/`(--[a-z0-9-]+)-\$\{/gi)) defined.add(m[1]);

    const missing = [];
    for (const [path, text] of css) {
      for (const m of text.matchAll(/var\(\s*(--[a-z0-9-]+)/gi)) {
        const name = m[1];
        if (defined.has(name) || RUNTIME_ONLY.has(name)) continue;
        if ([...defined].some((d) => name.startsWith(`${d}-`) && d.endsWith('-'))) continue;
        missing.push(`${path.slice(SRC.length)}: ${name}`);
      }
    }
    expect([...new Set(missing)]).toEqual([]);
  });
});
