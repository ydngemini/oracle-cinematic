import { describe, expect, it } from 'vitest';
import { EMAIL_PRESETS, explainEmailError, guessPreset, presetById, smtpPayload } from './emailPresets';

describe('email presets', () => {
  it('fills the server for a known provider and logs in as the address', () => {
    const payload = smtpPayload(presetById('gmail'), { email: ' jane@gmail.com ', password: 'abcd efgh ijkl mnop', from_name: 'Jane' });
    expect(payload).toEqual({
      account_label: 'default', host: 'smtp.gmail.com', port: 587, username: 'jane@gmail.com',
      password: 'abcdefghijklmnop', from_email: 'jane@gmail.com', from_name: 'Jane',
    });
  });

  it('keeps spaces in a real password (only app passwords are grouped)', () => {
    expect(smtpPayload(presetById('m365'), { email: 'a@b.com', password: 'my pass phrase' }).password).toBe('my pass phrase');
  });

  it('uses what was typed for another server', () => {
    const payload = smtpPayload(presetById('other'), { email: 'a@brokerage.com', password: 'p', host: 'mail.brokerage.com', port: '465', username: 'a' });
    expect([payload.host, payload.port, payload.username]).toEqual(['mail.brokerage.com', 465, 'a']);
  });

  it('recognises the provider from the address', () => {
    expect(['x@gmail.com', 'x@icloud.com', 'x@yahoo.co.uk', 'x@hotmail.com', 'x@brokerage.com'].map(guessPreset))
      .toEqual(['gmail', 'icloud', 'yahoo', 'outlook', null]);
  });

  it('says personal Outlook cannot connect with a password, instead of letting it fail', () => {
    expect(presetById('outlook').unsupported).toMatch(/September 2024/);
    expect(EMAIL_PRESETS.filter((p) => !p.unsupported).every((p) => p.steps.length && p.passwordLabel)).toBe(true);
  });

  it('turns a login refusal into what to do', () => {
    expect(explainEmailError('(535, b"5.7.8 Username and Password not accepted")', presetById('gmail'))).toMatch(/app password/);
    expect(explainEmailError('SMTP host "x" does not resolve', null)).toMatch(/couldn.t reach/);
  });
});
