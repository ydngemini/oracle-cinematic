import { describe, expect, it } from 'vitest';

import {
  CHROME_COLOR, DEFAULT_THEME, STORAGE_KEY, applyTheme, isTheme, nextTheme, readTheme, resolveTheme,
} from './theme';

const memory = (initial = {}) => {
  const store = { ...initial };
  return { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
};

describe('readTheme', () => {
  it('is light by default — the white must be seen without touching the OS', () => {
    expect(DEFAULT_THEME).toBe('light');
    expect(readTheme(memory())).toBe('light');
    expect(readTheme(null)).toBe('light');
  });

  it('honours a stored choice and ignores garbage', () => {
    expect(readTheme(memory({ [STORAGE_KEY]: 'dark' }))).toBe('dark');
    expect(readTheme(memory({ [STORAGE_KEY]: 'system' }))).toBe('system');
    expect(readTheme(memory({ [STORAGE_KEY]: 'neon' }))).toBe('light');
  });

  it('survives storage that throws', () => {
    expect(readTheme({ getItem: () => { throw new Error('denied'); } })).toBe('light');
  });
});

describe('resolveTheme / nextTheme', () => {
  it('system follows the OS; explicit choices do not', () => {
    expect(resolveTheme('system', true)).toBe('dark');
    expect(resolveTheme('system', false)).toBe('light');
    expect(resolveTheme('light', true)).toBe('light');
    expect(resolveTheme('dark', false)).toBe('dark');
  });

  it('a toggle always visibly changes something, even from system', () => {
    expect(nextTheme('light', true)).toBe('dark');
    expect(nextTheme('dark', true)).toBe('light');
    expect(nextTheme('system', true)).toBe('light');
    expect(nextTheme('system', false)).toBe('dark');
  });

  it('knows exactly three themes', () => {
    expect(isTheme('light') && isTheme('dark') && isTheme('system')).toBe(true);
    expect(isTheme('auto')).toBe(false);
  });
});

describe('applyTheme', () => {
  const fakeDoc = () => {
    const attrs = {};
    let chrome = '';
    return {
      documentElement: {
        setAttribute: (k, v) => { attrs[k] = v; },
        removeAttribute: (k) => { delete attrs[k]; },
      },
      querySelector: () => ({ setAttribute: (_k, v) => { chrome = v; } }),
      attrs: () => attrs,
      chrome: () => chrome,
    };
  };

  it('stamps light and dark, and removes the stamp for system', () => {
    const doc = fakeDoc();
    applyTheme('dark', doc);
    expect(doc.attrs()['data-theme']).toBe('dark');
    expect(doc.chrome()).toBe(CHROME_COLOR.dark);
    applyTheme('light', doc);
    expect(doc.attrs()['data-theme']).toBe('light');
    expect(doc.chrome()).toBe(CHROME_COLOR.light);
    applyTheme('system', doc);
    expect('data-theme' in doc.attrs()).toBe(false);
  });

  it('does nothing without a document', () => {
    expect(() => applyTheme('dark', null)).not.toThrow();
  });
});
