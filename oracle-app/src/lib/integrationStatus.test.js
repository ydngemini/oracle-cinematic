import { describe, expect, it } from 'vitest';
import { canonicalState, integrationStatus } from './integrationStatus';

describe('integrationStatus — customer words, never raw provider state', () => {
  it.each([
    ['phone', 'READY', 'Ready'],
    ['messaging', 'NEEDS_ACTION', 'Needs verification'],
    ['mls', 'IN_PROGRESS', 'Syncing'],
    ['calendar', 'ERROR', 'Reconnect'],
    ['email_calendar', 'ERROR', 'Reconnect'],
    ['phone', 'unvalidated', 'Needs verification'],
    ['phone', 'invalid', 'Unavailable'],
    ['email', null, 'Not set up'],
    ['billing', 'NEEDS_ACTION', 'Add payment details'],
    ['contact_import', 'NEEDS_ACTION', 'Needs you'],
  ])('%s %s → %s', (integration, state, label) => {
    expect(integrationStatus(integration, state).label).toBe(label);
  });

  it('never leaks an unknown vendor state', () => {
    const status = integrationStatus('phone', 'twilio_error_20003');
    expect(status.label).toBe('Unavailable');
    expect(status.label).not.toMatch(/twilio|_/i);
    expect(integrationStatus('contacts', 'weird_state').label).toBe('Needs attention');
  });

  it('folds provider validation states into canonical ones', () => {
    expect(canonicalState('valid')).toBe('READY');
    expect(canonicalState('setup_required')).toBe('NOT_STARTED');
    expect(canonicalState('pending')).toBe('IN_PROGRESS');
    expect(canonicalState('')).toBe('NOT_STARTED');
  });

  it('every status carries a tone, so the badge can pick an icon as well as a colour', () => {
    for (const state of ['READY', 'NOT_STARTED', 'NEEDS_ACTION', 'IN_PROGRESS', 'BLOCKED', 'ERROR', 'OPTIONAL']) {
      expect(['ready', 'progress', 'attention', 'problem', 'idle']).toContain(integrationStatus('mls', state).tone);
    }
  });
});
