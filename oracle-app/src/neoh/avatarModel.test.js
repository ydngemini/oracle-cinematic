import { describe, expect, it } from 'vitest';

import { actionTypeFor, avatarState, smoothLevel } from './avatarModel';

describe('avatarState', () => {
  it('is idle when nothing is happening', () => {
    expect(avatarState({}).state).toBe('idle');
  });

  it('thinks while a message is pending', () => {
    expect(avatarState({ messages: [{ status: 'pending' }] }).state).toBe('thinking');
  });

  it('thinks while the deterministic ask path is in flight', () => {
    expect(avatarState({ asking: true }).state).toBe('thinking');
  });

  it('listens when the person is speaking to Neoh', () => {
    expect(avatarState({ micActive: true }).state).toBe('listening');
  });

  it('speaks while assistant audio is playing', () => {
    expect(avatarState({ speaking: true }).state).toBe('speaking');
  });

  it('acts while a tool is executing, and classifies the action', () => {
    const result = avatarState({ actionName: 'place_call' });
    expect(result.state).toBe('acting');
    expect(result.actionType).toBe('call');
  });

  it('shows needs_attention when the person must act', () => {
    expect(avatarState({ attention: true }).state).toBe('needs_attention');
  });

  it('shows error on a genuine failure', () => {
    expect(avatarState({ failed: true }).state).toBe('error');
  });

  it('reports disconnected rather than faking activity on a dead channel', () => {
    // A pending message plus a dead socket is NOT thinking — the request is
    // not going anywhere, and a thinking face there would be a lie.
    const result = avatarState({ connection: 'offline', messages: [{ status: 'pending' }] });
    expect(result.state).toBe('disconnected');
  });

  describe('precedence', () => {
    it('error outranks everything', () => {
      expect(avatarState({
        failed: true, attention: true, speaking: true, micActive: true, actionName: 'call',
      }).state).toBe('error');
    });

    it('needs_attention outranks live channel states', () => {
      expect(avatarState({ attention: true, speaking: true, micActive: true }).state).toBe('needs_attention');
    });

    it('speaking outranks listening', () => {
      expect(avatarState({ speaking: true, micActive: true }).state).toBe('speaking');
    });

    it('listening outranks acting and thinking', () => {
      expect(avatarState({
        micActive: true, actionName: 'send_sms', messages: [{ status: 'pending' }],
      }).state).toBe('listening');
    });

    it('acting outranks thinking', () => {
      expect(avatarState({
        actionName: 'send_sms', messages: [{ status: 'pending' }],
      }).state).toBe('acting');
    });

    it('thinking outranks a held success', () => {
      expect(avatarState({ messages: [{ status: 'streaming' }], succeeded: true }).state).toBe('thinking');
    });
  });
});

describe('actionTypeFor', () => {
  it.each([
    ['place_call', 'call'],
    ['dial_contact', 'call'],
    ['send_sms', 'message'],
    ['send_email', 'message'],
    ['create_showing', 'calendar'],
    ['schedule_appointment', 'calendar'],
    ['search_listings', 'property'],
    ['property_lookup', 'property'],
    ['research_market', 'search'],
    ['save_note', 'save'],
    ['share_property_tour', 'share'],
  ])('maps %s to %s', (name, expected) => {
    expect(actionTypeFor(name)).toBe(expected);
  });

  it('falls back to generic rather than leaking an unknown tool name', () => {
    expect(actionTypeFor('some_internal_tool_v2')).toBe('generic');
    expect(actionTypeFor('')).toBe('generic');
    expect(actionTypeFor(null)).toBe('generic');
  });

  it('never surfaces a carrier name as an action type', () => {
    // The mascot must not know Plivo/Telnyx/Twilio/Qwen exist.
    for (const carrier of ['plivo', 'telnyx', 'twilio', 'qwen']) {
      expect(['call', 'message', 'generic']).toContain(actionTypeFor(carrier));
    }
  });
});

describe('smoothLevel', () => {
  it('rises faster than it falls', () => {
    const up = smoothLevel(0, 1);
    const down = smoothLevel(1, 0);
    expect(up).toBeGreaterThan(0);
    expect(1 - down).toBeLessThan(up);
  });

  it('clamps to 0..1', () => {
    expect(smoothLevel(0, 5)).toBeLessThanOrEqual(1);
    expect(smoothLevel(0, -5)).toBeGreaterThanOrEqual(0);
  });

  it('tolerates junk input', () => {
    expect(Number.isFinite(smoothLevel(undefined, undefined))).toBe(true);
    expect(Number.isFinite(smoothLevel(NaN, 'x'))).toBe(true);
  });
});
