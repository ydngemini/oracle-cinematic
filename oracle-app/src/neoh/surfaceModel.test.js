import { describe, expect, it } from 'vitest';

import { STATES, inputPlaceholder, isBusy, restLabel, surfaceState } from './surfaceModel';

const msg = (role, status) => ({ role, status });

describe('surfaceState', () => {
  it('is a pill at rest and yields to a sheet', () => {
    expect(surfaceState({ open: false, entityOpen: false, messages: [] })).toBe('rest');
    expect(surfaceState({ open: false, entityOpen: true, messages: [] })).toBe('yielded');
  });

  it('opens as input, holds as thinking while a reply is live, and shows results when asked', () => {
    expect(surfaceState({ open: true, entityOpen: false, messages: [] })).toBe('input');
    expect(surfaceState({ open: true, entityOpen: false, messages: [msg('assistant', 'pending')] })).toBe('thinking');
    expect(surfaceState({ open: true, entityOpen: false, messages: [msg('assistant', 'streaming')], showResult: true })).toBe('thinking');
    expect(surfaceState({ open: true, entityOpen: false, messages: [msg('assistant', 'completed')], showResult: true })).toBe('result');
    expect(surfaceState({ open: true, entityOpen: false, messages: [], showResult: true })).toBe('input');
  });

  it('a person who opens Neoh over a sheet gets Neoh, not the sheet', () => {
    expect(surfaceState({ open: true, entityOpen: true, messages: [] })).toBe('input');
  });

  it('knows exactly five shapes', () => {
    expect(STATES).toEqual(['rest', 'input', 'thinking', 'result', 'yielded']);
  });
});

describe('labels', () => {
  it('says what Neoh is looking at', () => {
    expect(restLabel({ record: { label: 'Sarah Chen' }, messages: [] })).toBe('Ask about Sarah Chen');
    expect(restLabel({ record: null, messages: [] })).toBe('Ask Neoh');
    expect(restLabel({ record: null, messages: [msg('assistant', 'completed')] })).toBe('Neoh answered');
    expect(restLabel({ record: { label: 'x' }, messages: [], busy: true })).toBe('Neoh is working…');
    expect(inputPlaceholder({ label: '12 Main St' })).toBe('Ask about 12 Main St');
    expect(inputPlaceholder(null)).toMatch(/anything/);
  });

  it('busy means a pending or streaming message, nothing else', () => {
    expect(isBusy([msg('user', 'completed'), msg('assistant', 'streaming')])).toBe(true);
    expect(isBusy([msg('assistant', 'completed')])).toBe(false);
    expect(isBusy(null)).toBe(false);
  });
});
