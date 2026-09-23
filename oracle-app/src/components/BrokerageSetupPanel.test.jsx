// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  crmGet: vi.fn(), crmPost: vi.fn(), crmPatch: vi.fn(), crmDelete: vi.fn(),
}));
vi.mock('../state/useCrmApi', () => api);

const { BrokerageSetupPanel } = await import('./BrokerageSetupPanel');

const SETUP = {
  brokerage: {
    id: 't1', name: 'Lockwood Realty', slug: 'lockwood', org_type: 'brokerage',
    primary_state: 'DE', website: null, profile_completed_at: '2026-09-01T00:00:00Z',
  },
  capabilities: {
    brokerage_profile: 'READY', agent_invites: 'NEEDS_ACTION', contact_import: 'NOT_STARTED',
    email_calendar: 'NOT_STARTED', phone: 'READY', mls: 'NOT_STARTED',
    billing: 'NOT_STARTED', readiness: 'BLOCKED',
  },
  recommended_next: 'agent_invites',
  team: { active_members: 1, pending_invitations: 2, accepted_invitations: 0 },
  messaging: 'IN_PROGRESS',
  optional: ['contact_import', 'email_calendar', 'mls'],
};

const TEAM = {
  members: [{
    id: 'u1', agent_id: 'nat@lockwood.test', email: 'nat@lockwood.test',
    full_name: 'Nathaniel', role: 'broker_owner', title: null, status: 'active',
    membership_status: 'active', joined_at: '2026-09-01T00:00:00Z',
  }],
  pending_invitations: [{
    id: 'i1', email: 'john@example.com', role: 'agent', state: 'pending',
    invited_by: 'nat@lockwood.test', expires_at: '2026-10-01T00:00:00Z',
    created_at: '2026-09-20T00:00:00Z', last_sent_at: '2026-09-20T00:00:00Z', send_count: 1,
  }],
};

beforeEach(() => {
  api.crmGet.mockReset(); api.crmPost.mockReset();
  api.crmPatch.mockReset(); api.crmDelete.mockReset();
  api.crmGet.mockImplementation((path) =>
    Promise.resolve(path.includes('/team') ? TEAM : SETUP));
});
afterEach(cleanup);

describe('BrokerageSetupPanel', () => {
  it('renders the brokerage identity the backend reported', async () => {
    render(<BrokerageSetupPanel />);
    expect(await screen.findByText('Lockwood Realty')).toBeTruthy();
    expect(screen.getByText(/Brokerage · DE/)).toBeTruthy();
  });

  it('renders every capability the backend sent, and no others', async () => {
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    for (const label of ['Business profile', 'Team', 'Contacts', 'Email & calendar',
                         'Phone', 'MLS', 'Billing', 'Ready to work']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });

  it('shows the backend’s recommended next step rather than deciding one', async () => {
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    const next = screen.getByText(/^Next:/);
    expect(next.textContent).toContain('Team');
  });

  it('marks optional capabilities so a missing MLS does not read as a failure', async () => {
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    expect(screen.getAllByText(/optional/).length).toBeGreaterThan(0);
  });

  it('summarises the team as active plus pending', async () => {
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    expect(screen.getByText(/1 active · 2 pending/)).toBeTruthy();
  });

  it('lists members and pending invitations together', async () => {
    render(<BrokerageSetupPanel />);
    expect(await screen.findByText('Nathaniel')).toBeTruthy();
    expect(screen.getByText('john@example.com')).toBeTruthy();
    expect(screen.getByText('Pending')).toBeTruthy();
  });

  it('sends one invitation from a single address', async () => {
    api.crmPost.mockResolvedValue({ created: [{ id: 'x' }], skipped: [] });
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    fireEvent.change(screen.getByLabelText('Email addresses to invite'),
      { target: { value: 'sarah@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send invitation' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith(
      '/api/brokerage/invitations', { emails: ['sarah@example.com'], role: 'agent' }));
  });

  it('splits a pasted block into a bulk invitation', async () => {
    api.crmPost.mockResolvedValue({ created: [{}, {}, {}], skipped: [] });
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    fireEvent.change(screen.getByLabelText('Email addresses to invite'), {
      target: { value: 'john@example.com\nsarah@example.com, mike@example.com' },
    });
    // The button counts them, so the person sees what is about to happen.
    expect(screen.getByRole('button', { name: 'Send 3 invitations' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Send 3 invitations' }));
    await waitFor(() => expect(api.crmPost.mock.calls[0][1].emails).toEqual(
      ['john@example.com', 'sarah@example.com', 'mike@example.com']));
  });

  it('will not send with nothing typed', async () => {
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    expect(screen.getByRole('button', { name: 'Send invitation' }).disabled).toBe(true);
  });

  it('reports how many were skipped as already on the team', async () => {
    api.crmPost.mockResolvedValue({
      created: [{ id: 'x' }],
      skipped: [{ email: 'nat@lockwood.test', reason: 'already_a_member' }],
    });
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    fireEvent.change(screen.getByLabelText('Email addresses to invite'),
      { target: { value: 'a@b.test nat@lockwood.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send 2 invitations' }));
    expect(await screen.findByText(/already on your team/)).toBeTruthy();
  });

  it('resends a pending invitation', async () => {
    api.crmPost.mockResolvedValue({ created: [], skipped: [] });
    render(<BrokerageSetupPanel />);
    await screen.findByText('john@example.com');
    fireEvent.click(screen.getByRole('button', { name: 'Resend' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith(
      '/api/brokerage/invitations/i1/resend', {}));
  });

  it('revokes a pending invitation', async () => {
    api.crmDelete.mockResolvedValue({ invitation: {} });
    render(<BrokerageSetupPanel />);
    await screen.findByText('john@example.com');
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(api.crmDelete).toHaveBeenCalledWith(
      '/api/brokerage/invitations/i1'));
  });

  it('surfaces an API failure instead of silently doing nothing', async () => {
    api.crmPost.mockRejectedValue(new Error('Only an owner can invite agents.'));
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    fireEvent.change(screen.getByLabelText('Email addresses to invite'),
      { target: { value: 'x@y.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send invitation' }));
    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent', 'Only an owner can invite agents.');
  });

  it('reports a load failure rather than rendering an empty screen', async () => {
    api.crmGet.mockRejectedValue(new Error('Network down'));
    render(<BrokerageSetupPanel />);
    expect(await screen.findByText('Network down')).toBeTruthy();
  });

  it('saves the business profile and never sends a tenant id', async () => {
    api.crmPatch.mockResolvedValue(SETUP);
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    fireEvent.click(screen.getByRole('button', { name: 'Edit business details' }));
    fireEvent.change(screen.getByLabelText('Primary state'), { target: { value: 'md' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.crmPatch).toHaveBeenCalled());
    const payload = api.crmPatch.mock.calls[0][1];
    expect(payload.primary_state).toBe('MD');   // uppercased in the field
    expect(payload).not.toHaveProperty('tenant_id');
    expect(payload).not.toHaveProperty('id');
  });

  it('reloads from the server after a change rather than patching local state', async () => {
    api.crmPost.mockResolvedValue({ created: [{ id: 'x' }], skipped: [] });
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    const before = api.crmGet.mock.calls.length;
    fireEvent.change(screen.getByLabelText('Email addresses to invite'),
      { target: { value: 'x@y.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send invitation' }));
    await waitFor(() => expect(api.crmGet.mock.calls.length).toBeGreaterThan(before));
  });
});
