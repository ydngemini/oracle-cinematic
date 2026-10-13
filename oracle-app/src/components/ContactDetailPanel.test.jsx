// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn(), crmPatch: vi.fn(), crmDelete: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: ContactDetailPanel } = await import('./ContactDetailPanel');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const PEOPLE = [
  { user_id: 'u-o', agent_id: 'jordan@nb.test', role: 'broker_owner', name: 'Jordan Miles', is_me: false },
  { user_id: 'u-a', agent_id: 'avery@nb.test', role: 'agent', name: 'Avery Chen', is_me: false },
  { user_id: 'u-b', agent_id: 'blake@nb.test', role: 'agent', name: 'Blake Ruiz', is_me: false },
];
const as = (meId) => PEOPLE.map((p) => ({ ...p, is_me: p.user_id === meId }));

function respond({ contact, access, people }) {
  api.crmGet.mockImplementation((path) => {
    if (path.endsWith('/access')) return Promise.resolve(access);
    if (path === '/api/crm/colleagues') return Promise.resolve({ colleagues: people });
    if (path.startsWith('/api/crm/intake/questions/')) return Promise.resolve({ questions: ['Q1', 'Q2', 'Q3'], version: 'v1' });
    return Promise.resolve({ contact });
  });
}

const CONTACT = {
  id: 'k1', full_name: 'Sarah Johnson', email: 's@example.test', phone: '+13025550142',
  assigned_agent_id: 'avery@nb.test', pool: false, preferred_channel: 'none',
  consent: {}, suppression: {}, nurture_enabled: true,
};

describe('contact access (0134, D-2026-10-13-01)', () => {
  it('shows a view-only share as view only: nothing to save, no intake, no nurture', async () => {
    respond({
      contact: { ...CONTACT, access: 'view' },
      access: { access: 'view', pool: false, can_manage_shares: false, shares: [], assignee_id: 'avery@nb.test' },
      people: as('u-b'),
    });
    render(<ContactDetailPanel contactId="k1" onClose={() => {}} />);
    expect(await screen.findByText('Shared with you by Avery Chen: view only.')).toBeTruthy();
    expect(screen.getByText('Who can see this contact')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save changes' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reserve next nurture touch' })).toBeNull();
    expect(screen.getByDisplayValue('Sarah Johnson').readOnly).toBe(true);
    expect(api.crmGet).toHaveBeenCalledWith('/api/crm/contacts/k1/access');
  });

  it('lets the owner assign the contact, and sends the canonical agent id', async () => {
    respond({
      contact: { ...CONTACT, access: 'edit' },
      access: { access: 'edit', pool: false, can_manage_shares: true, shares: [], assignee_id: 'avery@nb.test' },
      people: as('u-o'),
    });
    api.crmPatch.mockResolvedValue({ contact: { ...CONTACT, assigned_agent_id: 'blake@nb.test' } });
    render(<ContactDetailPanel contactId="k1" onClose={() => {}} />);
    const picker = await screen.findByRole('combobox', { name: 'Assign this contact' });
    fireEvent.change(picker, { target: { value: 'blake@nb.test' } });
    await waitFor(() => expect(api.crmPatch).toHaveBeenCalledWith('/api/crm/contacts/k1', { assigned_agent_id: 'blake@nb.test' }));
  });

  it('shares through the contact routes, not the client ones', async () => {
    respond({
      contact: { ...CONTACT, access: 'edit' },
      access: { access: 'edit', pool: false, can_manage_shares: true, shares: [], assignee_id: 'avery@nb.test' },
      people: as('u-a'),
    });
    api.crmPost.mockResolvedValue({});
    render(<ContactDetailPanel contactId="k1" onClose={() => {}} />);
    const who = await screen.findByRole('combobox', { name: 'Share with' });
    fireEvent.change(who, { target: { value: 'blake@nb.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith(
      '/api/crm/contacts/k1/shares', { grantee: 'blake@nb.test', permission: 'view' },
    ));
  });
});
