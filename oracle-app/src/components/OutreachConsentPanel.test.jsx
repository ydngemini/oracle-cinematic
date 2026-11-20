// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmPost: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: OutreachConsentPanel } = await import('./OutreachConsentPanel');

beforeEach(() => {
  api.crmPost.mockReset();
  api.crmPost.mockResolvedValue({});
});
afterEach(cleanup);

const type = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const record = () => fireEvent.click(screen.getByRole('button', { name: 'Record consent' }));
const consentCall = () => api.crmPost.mock.calls.find(([url]) => url === '/api/compliance/outreach/consent');

describe('OutreachConsentPanel evidence (external audit 26.6)', () => {
  it('asks for the capture method and evidence reference for written consent and sends them', async () => {
    api.crmPost.mockResolvedValue({ consent_id: 'c1', evidence_status: 'documented', evidence_notice: null });
    render(<OutreachConsentPanel />);
    type('Contact (phone or email)', '+13025550142');
    type('How it was captured', 'e_signature');
    type('Disclosure text the person agreed to', ' I agree to marketing texts. ');
    type('Evidence reference', ' envelope-123 ');
    record();
    await waitFor(() => expect(consentCall()).toBeTruthy());
    expect(consentCall()[1]).toEqual({
      contact: '+13025550142', channel: 'sms', consent_type: 'express_written',
      proof_text: 'I agree to marketing texts.', proof_source: 'e_signature', evidence_ref: 'envelope-123',
    });
    expect(await screen.findByText('Consent recorded with documented evidence.')).toBeTruthy();
    expect(screen.queryByText(/unverified claim/)).toBeNull();
  });

  it('never sends a status: the server decides', async () => {
    render(<OutreachConsentPanel />);
    type('Contact (phone or email)', '+13025550142');
    record();
    await waitFor(() => expect(consentCall()).toBeTruthy());
    expect(consentCall()[1]).not.toHaveProperty('evidence_status');
    expect(consentCall()[1]).not.toHaveProperty('recorded_by');
    expect(consentCall()[1]).not.toHaveProperty('proof_source');
  });

  it('says plainly that an unverified claim authorises nothing yet', async () => {
    api.crmPost.mockResolvedValue({
      consent_id: 'c2', evidence_status: 'attested_unverified',
      evidence_notice: 'Recorded as attested, not documented.',
    });
    render(<OutreachConsentPanel />);
    type('Contact (phone or email)', '+13025550142');
    record();
    const status = await screen.findByText(/Recorded as an unverified claim/);
    expect(status.textContent).toMatch(/authorises no AI call, no voiceprint recording and no marketing text/);
    expect(screen.getByText(/Recorded as attested, not documented\./)).toBeTruthy();
    expect(screen.queryByText('Consent recorded with documented evidence.')).toBeNull();
    expect(screen.queryByText('Consent recorded.')).toBeNull();
  });

  it('treats a response without a status as not documented', async () => {
    api.crmPost.mockResolvedValue({ consent_id: 'c3' });
    render(<OutreachConsentPanel />);
    type('Contact (phone or email)', '+13025550142');
    record();
    expect(await screen.findByText(/Recorded as an unverified claim/)).toBeTruthy();
  });

  it('asks for evidence for a voiceprint release and sends it on the voice channel', async () => {
    api.crmPost.mockResolvedValue({ consent_id: 'c4', evidence_status: 'documented' });
    render(<OutreachConsentPanel />);
    type('Contact (phone or email)', '(312) 555-0142');
    type('Consent type', 'biometric_voiceprint');
    type('How it was captured', 'recorded_call');
    type('Disclosure text the person agreed to', 'May we record this call?');
    type('Evidence reference', 'rec-9');
    record();
    await waitFor(() => expect(consentCall()).toBeTruthy());
    expect(consentCall()[1]).toMatchObject({
      channel: 'voice', consent_type: 'biometric_voiceprint', proof_source: 'recorded_call', evidence_ref: 'rec-9',
    });
  });

  it('hides the evidence fields for oral consent and sends none of them', async () => {
    render(<OutreachConsentPanel />);
    type('Contact (phone or email)', '+13025550142');
    type('How it was captured', 'web_form');
    type('Evidence reference', 'form-1');
    type('Consent type', 'express_oral');
    expect(screen.queryByLabelText('How it was captured')).toBeNull();
    expect(screen.queryByLabelText('Evidence reference')).toBeNull();
    record();
    await waitFor(() => expect(consentCall()).toBeTruthy());
    expect(consentCall()[1]).toEqual({ contact: '+13025550142', channel: 'sms', consent_type: 'express_oral' });
    expect(await screen.findByText('Consent recorded.')).toBeTruthy();
  });
});

describe('OutreachConsentPanel check (external audit 26.5)', () => {
  it('sends the message type and shows why a text is blocked', async () => {
    api.crmPost.mockResolvedValue({
      allowed: false, blockers: ['no documented prior express WRITTEN consent on file for a marketing text (sms)'],
    });
    render(<OutreachConsentPanel />);
    type('Contact (phone or email)', '+13025550142');
    fireEvent.click(screen.getByRole('button', { name: 'Check before sending' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/compliance/outreach/check', {
      contact: '+13025550142', channel: 'sms', state_code: null, purpose: 'marketing',
    }));
    expect(await screen.findByText(/Blocked\..*documented prior express WRITTEN consent/)).toBeTruthy();

    type('Message type (for the check)', 'informational');
    fireEvent.click(screen.getByRole('button', { name: 'Check before sending' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenLastCalledWith('/api/compliance/outreach/check',
      expect.objectContaining({ purpose: 'informational' })));
  });
});
