import { describe, expect, it } from 'vitest';

import { emailLabel } from './emailChannel';

describe('the Email card says what a send proved (T10.09, L10-11)', () => {
  it('is Ready only when the backend says a sender works', () => {
    expect(emailLabel(true, 'not_yet_verified')).toBe('Ready');
    expect(emailLabel(false, 'not_yet_verified')).toBe('Not yet verified by a send');
    expect(emailLabel(false, 'failing')).toBe('Last send failed');
    expect(emailLabel(false, 'not_configured')).toBe('Not set up');
    expect(emailLabel(false, undefined)).toBe('Not set up');
  });
});
