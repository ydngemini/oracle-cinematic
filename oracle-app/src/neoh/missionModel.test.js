import { describe, expect, it } from 'vitest';

import {
  AUTOPILOT_CHANNELS,
  CONSENT_SENTENCES,
  VOICE_CAVEAT,
  consentText,
  statusLine,
  toRequest,
  validate,
} from './missionModel';

describe('consent', () => {
  it('composes the exact sentence that gets stored', () => {
    // The words on screen and the words in the database are the same string.
    // Templating it server-side would let those two drift.
    const text = consentText(['sms']);
    expect(text).toBe(CONSENT_SENTENCES.sms);
    expect(toRequest({ objectiveText: 'x', autoChannels: ['sms'] }).consent_text).toBe(text);
  });

  it('says the three things that actually matter', () => {
    for (const channel of AUTOPILOT_CHANNELS) {
      const sentence = CONSENT_SENTENCES[channel];
      expect(sentence, channel).toMatch(/under my licence/);
      expect(sentence, channel).toMatch(/cannot be un-/);
      expect(sentence, channel).toMatch(/revoke this by pausing/);
    }
  });

  it('is empty when nothing is on autopilot, so no consent is recorded', () => {
    expect(consentText([])).toBe('');
    expect(toRequest({ objectiveText: 'x', autoChannels: [] }).consent_text).toBeNull();
  });

  it('a task can never be put on autopilot — it reaches nobody', () => {
    expect(AUTOPILOT_CHANNELS).not.toContain('task');
    expect(toRequest({ objectiveText: 'x', autoChannels: ['task', 'sms'] }).auto_channels)
      .toEqual(['sms']);
  });

  it('warns that calls will reach far fewer people than the box implies', () => {
    // An agent who ticks "calls" expects calls. Mostly they will not happen,
    // because FCC 24-17 needs express written consent per contact. A mission
    // that looks live and does nothing is what erodes trust fastest.
    expect(VOICE_CAVEAT).toMatch(/express written consent/);
    expect(VOICE_CAVEAT).toMatch(/skipped with the reason recorded/);
    expect(VOICE_CAVEAT).toMatch(/fewer people than texts/);
  });
});

describe('validate', () => {
  it('refuses a mission with no stated outcome', () => {
    expect(validate({ allowedChannels: ['sms'] }).ok).toBe(false);
    expect(validate({ objectiveText: '   ', allowedChannels: ['sms'] }).problem)
      .toMatch(/what you want to happen/);
  });

  it('refuses autopilot on a channel the mission may not use', () => {
    const out = validate({
      objectiveText: 'win listings',
      allowedChannels: ['email'],
      autoChannels: ['sms'],
    });
    expect(out.ok).toBe(false);
    expect(out.problem).toMatch(/not allowed to use/);
  });

  it('accepts a complete mission', () => {
    expect(validate({
      objectiveText: 'Win three listings in Newark',
      allowedChannels: ['sms', 'email'],
      autoChannels: ['sms'],
    })).toEqual({ ok: true, problem: null });
  });
});

describe('toRequest', () => {
  it('converts dollars to cents and sorts channels for a stable diff', () => {
    const body = toRequest({
      objectiveKind: 'listings_won',
      objectiveText: '  Win three listings  ',
      budgetDollars: '12.34',
      allowedChannels: ['sms', 'email'],
      autoChannels: ['email', 'sms'],
    });
    expect(body.budget_cents).toBe(1234);
    expect(body.objective_text).toBe('Win three listings');
    expect(body.allowed_channels).toEqual(['email', 'sms']);
    expect(body.auto_channels).toEqual(['email', 'sms']);
  });

  it('sends null rather than 0 for an unset target', () => {
    expect(toRequest({ objectiveText: 'x' }).target_count).toBeNull();
  });
});

describe('statusLine', () => {
  it('says shadow is recording, not sending', () => {
    expect(statusLine({ status: 'shadow', mode: 'shadow', auto_channels: [] }))
      .toMatch(/recording, not sending/);
  });

  it('names exactly what goes without approval', () => {
    expect(statusLine({ status: 'active', mode: 'live', auto_channels: ['sms'] }))
      .toBe('live, sending sms on its own.');
    expect(statusLine({ status: 'active', mode: 'live', auto_channels: [] }))
      .toMatch(/everything waits for your approval/);
  });
});
