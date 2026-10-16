import { describe, expect, it } from 'vitest';

import { channelLabel, emailLabel } from './emailChannel';

describe('the Email card says what a send proved (T10.09, L10-11)', () => {
  it('is Ready only when the backend says a sender works', () => {
    expect(emailLabel(true, 'not_yet_verified')).toBe('Ready');
    expect(emailLabel(false, 'not_yet_verified')).toBe('Not yet verified by a send');
    expect(emailLabel(false, 'failing')).toBe('Last send failed');
    expect(emailLabel(false, 'not_configured')).toBe('Not set up');
    expect(emailLabel(false, undefined)).toBe('Not set up');
  });
});

describe('the Text messages and Neoh calls cards follow the same rule (T10.09, L10-12)', () => {
  it('reads the platform Twilio state, never an env flag', () => {
    expect(channelLabel(true, 'not_yet_verified')).toBe('Ready');
    expect(channelLabel(false, 'not_yet_verified')).toBe('Not yet verified by a send');
    expect(channelLabel(false, 'failing')).toBe('Last send failed');
    expect(channelLabel(false, 'not_configured')).toBe('Not set up');
    expect(channelLabel(false, null)).toBe('Not set up');
  });
});
