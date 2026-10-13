// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn(), crmPatch: vi.fn(), crmDelete: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: ClientSharing, AssigneeControl } = await import('./ClientAccess');
const { useClientAccess } = await import('../state/useClientAccess');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const PEOPLE = [
  { user_id: 'u-o', agent_id: 'jordan@nb.test', role: 'broker_owner', name: 'Jordan Miles', is_me: false },
  { user_id: 'u-a', agent_id: 'avery@nb.test', role: 'agent', name: 'Avery Chen', is_me: false },
  { user_id: 'u-b', agent_id: 'blake@nb.test', role: 'agent', name: 'Blake Ruiz', is_me: false },
];
const as = (meId) => PEOPLE.map((p) => ({ ...p, is_me: p.user_id === meId }));

function respond(access, people) {
  api.crmGet.mockImplementation((path) => Promise.resolve(path.endsWith('/access') ? access : { colleagues: people }));
}

function Harness({ assigneeId, onAssign = () => {} }) {
  const state = useClientAccess('c1');
  return (
    <>
      <AssigneeControl state={state} assigneeId={assigneeId} onAssign={onAssign} />
      <ClientSharing clientId="c1" state={state} assigneeId={assigneeId} />
    </>
  );
}

describe('client access (D-2026-10-13-01)', () => {
  it('says a pool client is visible to the whole brokerage, and lets an agent take it', async () => {
    respond({ access: 'edit', pool: true, can_manage_shares: false, shares: [] }, as('u-a'));
    const onAssign = vi.fn();
    render(<Harness assigneeId={null} onAssign={onAssign} />);
    expect(await screen.findByText(/everyone in the brokerage can see and edit/)).toBeTruthy();
    expect(screen.queryByRole('form')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Take it' }));
    expect(onAssign).toHaveBeenCalledWith('avery@nb.test');
  });

  it("tells an agent a colleague's client is shared with them view only, with no share form", async () => {
    respond({ access: 'view', pool: false, can_manage_shares: false, shares: [] }, as('u-b'));
    render(<Harness assigneeId="AVERY@nb.test" />);
    expect(await screen.findByText('Shared with you by Avery Chen: view only.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Share' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Take it' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Return to pool' })).toBeNull();
  });

  it('lets the assignee share view only or editable, and stop sharing', async () => {
    respond({
      access: 'edit', pool: false, can_manage_shares: true,
      shares: [{ id: 's1', grantee_agent_id: 'blake@nb.test', grantee_name: 'Blake Ruiz', permission: 'view' }],
    }, as('u-a'));
    api.crmPost.mockResolvedValueOnce({});
    api.crmDelete.mockResolvedValueOnce(null);
    render(<Harness assigneeId="avery@nb.test" />);
    expect(await screen.findByText('Blake Ruiz', { selector: 'li span' })).toBeTruthy();
    // The assignee is never offered as a grantee, and neither is the owner.
    const options = [...screen.getByRole('combobox', { name: /share with/i }).querySelectorAll('option')].map((o) => o.textContent);
    expect(options).toEqual(['Choose an agent', 'Blake Ruiz']);
    fireEvent.change(screen.getByRole('combobox', { name: /share with/i }), { target: { value: 'blake@nb.test' } });
    fireEvent.change(screen.getByRole('combobox', { name: /access/i }), { target: { value: 'edit' } });
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/crm/clients/c1/shares',
      { grantee: 'blake@nb.test', permission: 'edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stop sharing with Blake Ruiz' }));
    await waitFor(() => expect(api.crmDelete).toHaveBeenCalledWith('/api/crm/clients/c1/shares/s1'));
    expect(screen.getByRole('button', { name: 'Return to pool' })).toBeTruthy();
  });

  it("shows the server's reason when a share is refused", async () => {
    respond({ access: 'edit', pool: false, can_manage_shares: true, shares: [] }, as('u-a'));
    api.crmPost.mockRejectedValueOnce(Object.assign(new Error('Only the assigned agent or the broker owner can share this client.'), { status: 403 }));
    render(<Harness assigneeId="avery@nb.test" />);
    fireEvent.change(await screen.findByRole('combobox', { name: /share with/i }), { target: { value: 'blake@nb.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalled());
    expect(await screen.findByText('Only the assigned agent or the broker owner can share this client.')).toBeTruthy();
  });

  it('gives the owner a picker over every member, the pool first', async () => {
    respond({ access: 'edit', pool: false, can_manage_shares: true, shares: [] }, as('u-o'));
    const onAssign = vi.fn();
    render(<Harness assigneeId="avery@nb.test" onAssign={onAssign} />);
    const picker = await screen.findByRole('combobox', { name: 'Assign this client' });
    await waitFor(() => expect(picker.value).toBe('avery@nb.test'));
    expect(picker.querySelector('option').textContent).toBe('Brokerage pool (unassigned)');
    fireEvent.change(picker, { target: { value: '' } });
    expect(onAssign).toHaveBeenCalledWith(null);
  });
});
