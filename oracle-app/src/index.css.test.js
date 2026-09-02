import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/**
 * The dark theme is declared twice — once under prefers-color-scheme for the
 * OS default, once under [data-theme="dark"] for an explicit choice — because
 * there is no preprocessor to share one block between two selectors. A
 * duplicate drifts the first time someone edits one by hand. This test is the
 * only thing that stops that.
 */

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'index.css'), 'utf8');

function propertyNames(blockText) {
  return new Set([...blockText.matchAll(/^\s*(--[a-z0-9-]+)\s*:/gim)].map((m) => m[1]));
}

function blockAfter(marker) {
  const start = css.indexOf(marker);
  if (start < 0) throw new Error(`marker not found: ${marker}`);
  // A marker may end with the block's own brace; searching for a brace AFTER
  // it would walk the next rule instead.
  const braceSearchFrom = start + marker.length - (marker.endsWith('{') ? 1 : 0);
  const open = css.indexOf('{', braceSearchFrom);
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    if (css[i] === '}') {
      depth -= 1;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  throw new Error('unterminated block');
}

describe('theme tokens', () => {
  const light = propertyNames(blockAfter('\n:root {'));
  const osDark = propertyNames(blockAfter(':root:not([data-theme="light"])'));
  const explicitDark = propertyNames(blockAfter(':root[data-theme="dark"] {'));

  it('declares the two dark blocks with an identical set of properties', () => {
    expect([...osDark].sort()).toEqual([...explicitDark].sort());
    expect(osDark.size).toBeGreaterThan(30);
  });

  it('never gives a token its only definition in a dark block', () => {
    for (const name of osDark) {
      expect(light.has(name), `${name} is dark-only`).toBe(true);
    }
  });

  it('light is the default and dark follows the OS', () => {
    expect(css).toMatch(/color-scheme:\s*light dark/);
    expect(css).toMatch(/@media \(prefers-color-scheme: dark\)/);
  });

  it('keeps accent text off raw gold', () => {
    // #ffbc1f as text fails AA on white. The ink exists so components have
    // somewhere else to reach for accent text.
    expect(light.has('--oracle-accent-ink')).toBe(true);
    expect(osDark.has('--oracle-accent-ink')).toBe(true);
  });

  it('gives new surfaces an opaque token so glass stays on the header, deck and sheets', () => {
    for (const name of ['--oracle-surface', '--oracle-surface-raised', '--oracle-surface-sunken']) {
      expect(light.has(name)).toBe(true);
      expect(osDark.has(name)).toBe(true);
    }
  });

  it('replaced the hardcoded reduced-transparency colour with a token', () => {
    expect(css).not.toMatch(/prefers-reduced-transparency[\s\S]{0,200}rgba\(12, 12, 12, 0\.96\)/);
    expect(css).toMatch(/prefers-reduced-transparency[\s\S]{0,200}var\(--oracle-glass-solid\)/);
  });
});
