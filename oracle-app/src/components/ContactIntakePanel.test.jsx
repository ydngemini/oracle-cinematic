// @vitest-environment jsdom
/**
 * New contact → POST /api/crm/contacts.
 *
 * The server's ConsentGrant requires `captured_at` whenever `granted` is true
 * (contacts_api.py). The form sent a bare `{ granted: true }`, so creating a
 * contact with ANY consent box ticked failed with a 422 ("Some details need
 * another look") — in the browser journey that was every contact a person
 * agreed to be texted.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmPost: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);
const { default: ContactIntakePanel } = await import('./ContactIntakePanel');

afterEach(() => { cleanup(); api.crmPost.mockReset(); });

function fill(label, value) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe('ContactIntakePanel', () => {
  it('records when consent was captured for every ticked channel, and nothing for the rest', async () => {
    api.crmPost.mockResolvedValue({ contact: { id: 'c1' } });
    const onCreated = vi.fn();
    render(<ContactIntakePanel onCreated={onCreated} />);
    fill('Full name', 'Bianca Buyer');
    fill('Phone', '+1 302 555 0142');
    fireEvent.click(screen.getByLabelText('SMS'));
    fireEvent.click(screen.getByRole('button', { name: 'Create contact' }));

    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const [path, payload] = api.crmPost.mock.calls[0];
    expect(path).toBe('/api/crm/contacts');
    expect(Object.keys(payload.consent)).toEqual(['sms']);
    expect(payload.consent.sms.granted).toBe(true);
    // Timezone-qualified, as the server's validator requires.
    expect(payload.consent.sms.captured_at).toMatch(/^\d{4}-\d{2}-\d{2}T.*Z$/);
    expect(Number.isNaN(Date.parse(payload.consent.sms.captured_at))).toBe(false);
  });

  it('sends no consent block when nothing was ticked', async () => {
    api.crmPost.mockResolvedValue({ contact: { id: 'c2' } });
    render(<ContactIntakePanel onCreated={vi.fn()} />);
    fill('Full name', 'Nora Nobox');
    fireEvent.click(screen.getByRole('button', { name: 'Create contact' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalled());
    expect(api.crmPost.mock.calls[0][1].consent).toBeUndefined();
  });
});
