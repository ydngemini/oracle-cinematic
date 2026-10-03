import { describe, expect, it } from 'vitest';

import {
  STATES, connectionMessage, contextLabel, inputPlaceholder, isBusy, micControl, presenceLine,
  restLabel, surfaceState,
} from './surfaceModel';
import { wireContext } from './useNeohChannel';

describe('composer language', () => {
  it('names the context in full, and nothing when there is none', () => {
    expect(contextLabel({ label: '123 Main St' })).toBe('Talking about 123 Main St');
    expect(contextLabel(null)).toBe('');
    expect(inputPlaceholder(null)).toBe('Ask Neoh anything…');
  });

  it('never prints the raw channel state', () => {
    expect(connectionMessage('online')).toBe('');
    expect(connectionMessage('offline')).toBe('Neoh is reconnecting. Your work is saved.');
    expect(connectionMessage('reconnecting')).not.toMatch(/channel/i);
  });

  it('labels the microphone with its state and keeps it in the composer when unsupported', () => {
    expect(micControl({ supported: true, state: 'idle' })).toMatchObject({ label: 'Talk to Neoh', disabled: false });
    expect(micControl({ supported: true, state: 'listening' })).toMatchObject({ label: 'Stop listening', pressed: true });
    expect(micControl({ supported: true, state: 'requesting' }).label).toMatch(/permission/);
    expect(micControl({ supported: false, state: 'unsupported' })).toMatchObject({ disabled: true });
    expect(presenceLine({ connection: 'online', busy: true })).toBe('Thinking…');
    expect(presenceLine({ connection: 'offline', busy: true })).toBe('Reconnecting…');
  });
});

describe('wireContext', () => {
  const id = 'cccccccc-2222-4222-8222-222222222222';
  it('sends only what the backend ChatContext accepts', () => {
    expect(wireContext({ type: 'client', id })).toEqual({ type: 'client', id });
    // A property sheet registers the lead behind it; the wire calls it 'lead'.
    expect(wireContext({ type: 'property', id })).toEqual({ type: 'lead', id });
    // No transaction context exists; sending one rejected the whole message.
    expect(wireContext({ type: 'transaction', id })).toBeNull();
    expect(wireContext({ type: 'lead', id: 'parcel-12-34' })).toBeNull();
    expect(wireContext(null)).toBeNull();
  });
});

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
