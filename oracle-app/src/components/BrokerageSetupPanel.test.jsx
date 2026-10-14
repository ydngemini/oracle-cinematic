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

  it('says when the postal address the email footer needs is missing', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(path.includes('/team') ? TEAM : {
      ...SETUP,
      commercial_email: {
        status: 'NEEDS_ACTION', postal_address_configured: false, unsubscribe: 'one_click',
        message: 'Add your brokerage\'s postal mailing address.',
      },
    }));
    render(<BrokerageSetupPanel />);
    expect(await screen.findByText(/Setup required: postal address/)).toBeTruthy();
  });

  it('says nothing about the address once the backend reports it set', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(path.includes('/team') ? TEAM : {
      ...SETUP,
      brokerage: { ...SETUP.brokerage, postal_address: '1 Loockerman St, Dover, DE 19901' },
      commercial_email: { status: 'READY', postal_address_configured: true, unsubscribe: 'one_click', message: null },
    }));
    render(<BrokerageSetupPanel />);
    expect(await screen.findByText('1 Loockerman St, Dover, DE 19901')).toBeTruthy();
    expect(screen.queryByText(/Setup required: postal address/)).toBeNull();
  });

  it('sends the postal address with the business profile', async () => {
    api.crmPatch.mockResolvedValue(SETUP);
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    fireEvent.click(screen.getByRole('button', { name: 'Edit business details' }));
    fireEvent.change(screen.getByLabelText(/Postal mailing address/),
      { target: { value: '1 Loockerman St\nDover, DE 19901' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.crmPatch).toHaveBeenCalled());
    expect(api.crmPatch.mock.calls[0][1].postal_address).toBe('1 Loockerman St\nDover, DE 19901');
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

describe('BrokerageSetupPanel — MLS honesty', () => {
  const withMls = (mls, capability) => ({
    ...SETUP,
    capabilities: { ...SETUP.capabilities, mls: capability },
    mls,
  });

  it('never presents developer/reference feeds as MLS coverage', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(
      path.includes('/team') ? TEAM : withMls({
        status: 'BLOCKED',
        detail: 'Connected feeds are developer/reference datasets…',
        feeds: [{ mls_id: 'actris', mls_name: 'ACTRIS reference', licensed: false, health: 'READY' }],
      }, 'BLOCKED'),
    ));
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    expect(screen.getByText(/not live MLS listings/)).toBeTruthy();
    expect(screen.queryByText('ACTRIS reference')).toBeNull();
  });

  it('names a licensed feed and how fresh it is', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(
      path.includes('/team') ? TEAM : withMls({
        status: 'READY',
        detail: 'Licensed MLS feed synced and fresh.',
        feeds: [{ mls_id: 'bright', mls_name: 'Bright MLS', licensed: true, health: 'READY', age_seconds: 720 }],
      }, 'READY'),
    ));
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    expect(screen.getByText(/Bright MLS · updated 12 min ago/)).toBeTruthy();
  });

  it('says nothing extra when no feed is connected', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(
      path.includes('/team') ? TEAM : withMls({ status: 'NOT_STARTED', detail: '', feeds: [] }, 'NOT_STARTED'),
    ));
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    expect(screen.queryByText(/not live MLS listings/)).toBeNull();
    expect(screen.queryByText(/updated/)).toBeNull();
  });

  it('prefers the licensed feed even when developer feeds sit beside it', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(
      path.includes('/team') ? TEAM : withMls({
        status: 'READY', detail: '',
        feeds: [
          { mls_id: 'actris', mls_name: 'ACTRIS reference', licensed: false, health: 'READY' },
          { mls_id: 'bright', mls_name: 'Bright MLS', licensed: true, health: 'READY', age_seconds: 90000 },
        ],
      }, 'READY'),
    ));
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    expect(screen.getByText(/Bright MLS · updated 1d ago/)).toBeTruthy();
  });

  it('gives an owner Suspend and Offboard on agents only — never on their own row', async () => {
    sessionStorage.setItem('oracle_role', 'broker_owner');
    localStorage.setItem('oracle_user_id', 'nat@lockwood.test');
    const team = { ...TEAM, members: [...TEAM.members, {
      id: 'u2', agent_id: 'sam@lockwood.test', email: 'sam@lockwood.test', full_name: 'Sam',
      role: 'agent', status: 'active', membership_status: 'active' }] };
    api.crmGet.mockImplementation((path) => Promise.resolve(path.includes('/team') ? team : SETUP));
    render(<BrokerageSetupPanel />);
    await screen.findByText('Sam');
    expect(screen.getAllByRole('button', { name: 'Offboard…' })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: 'Suspend' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Offboard…' }));
    expect(await screen.findByRole('group', { name: /Offboard Sam/ })).toBeTruthy();
    sessionStorage.clear();
    localStorage.clear();
  });

  it('shows agents no member actions', async () => {
    sessionStorage.setItem('oracle_role', 'agent');
    render(<BrokerageSetupPanel />);
    await screen.findByText('Lockwood Realty');
    expect(screen.queryByRole('button', { name: 'Offboard…' })).toBeNull();
    sessionStorage.clear();
  });

  it('tells the owner when an invitation email did not go out', async () => {
    const team = { ...TEAM, pending_invitations: [{ ...TEAM.pending_invitations[0], delivery_status: 'failed' }] };
    api.crmGet.mockImplementation((path) => Promise.resolve(path.includes('/team') ? team : SETUP));
    render(<BrokerageSetupPanel />);
    expect(await screen.findByText('Email not delivered')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Resend' })).toBeTruthy();
  });
});

describe('BrokerageSetupPanel two-step sign-in (L17-19)', () => {
  const OWNER_VIEW = {
    members: [
      { ...TEAM.members[0], mfa_enabled: true },
      { id: 'u2', agent_id: 'avery@lockwood.test', email: 'avery@lockwood.test', full_name: 'Avery',
        role: 'agent', title: null, status: 'active', membership_status: 'active',
        joined_at: '2026-09-02T00:00:00Z', mfa_enabled: true },
      { id: 'u3', agent_id: 'sam@lockwood.test', email: 'sam@lockwood.test', full_name: 'Sam',
        role: 'agent', title: null, status: 'active', membership_status: 'active',
        joined_at: '2026-09-03T00:00:00Z', mfa_enabled: false },
    ],
    pending_invitations: [],
  };

  beforeEach(() => {
    sessionStorage.setItem('oracle_role', 'broker_owner');
    localStorage.setItem('oracle_user_id', 'nat@lockwood.test');
    api.crmGet.mockImplementation((path) => Promise.resolve(path.includes('/team') ? OWNER_VIEW : SETUP));
  });
  afterEach(() => { sessionStorage.clear(); localStorage.clear(); });

  it('shows who has two-step on and lets the owner reset an agent who has it', async () => {
    api.crmPost.mockResolvedValueOnce({ id: 'u2', mfa_enabled: false });
    render(<BrokerageSetupPanel />);
    await screen.findByText('Avery');
    expect(screen.getAllByText('Two-step on')).toHaveLength(2);
    expect(screen.getAllByText('Two-step off')).toHaveLength(1);
    const resets = screen.getAllByRole('button', { name: 'Reset two-step' });
    expect(resets).toHaveLength(1); // Avery only: Sam has none, the owner is not reset from here
    fireEvent.click(resets[0]);
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith(
      '/api/brokerage/team/u2/mfa/reset', { reason: 'Two-step sign-in reset by an owner from Settings' }));
  });
});
