// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmPost: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { OffboardMemberForm } = await import('./OffboardMemberForm');

const MEMBERS = [
  { id: 'u1', agent_id: 'owner@b.test', full_name: 'Owner', role: 'broker_owner', status: 'active' },
  { id: 'u2', agent_id: 'leaving@b.test', full_name: 'Leaving Agent', role: 'agent', status: 'active' },
  { id: 'u3', agent_id: 'gone@b.test', full_name: 'Suspended Agent', role: 'agent', status: 'suspended' },
];
const PLAN = {
  preview: true, successor: { agent_id: 'owner@b.test' },
  reassign: { clients: 4, agent_contacts: 3, client_tasks: 0 },
  cancel_pending: { command_executions: 2 }, telephony: 'moved_to_successor', warnings: [],
};

beforeEach(() => { api.crmPost.mockReset(); });
afterEach(cleanup);

describe('OffboardMemberForm', () => {
  it('previews first, shows exactly what will move, then needs the password', async () => {
    const onDone = vi.fn();
    api.crmPost.mockResolvedValueOnce(PLAN).mockResolvedValueOnce({ preview: false });
    render(<OffboardMemberForm member={MEMBERS[1]} members={MEMBERS} onDone={onDone} onCancel={() => {}} />);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'left the brokerage' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    const plan = await screen.findByTestId('offboard-plan');
    expect(plan.textContent).toMatch('4 clients, 3 contacts');
    expect(plan.textContent).toMatch('2 command executions');
    expect(plan.textContent).toMatch('keep their name');
    expect(api.crmPost).toHaveBeenLastCalledWith('/api/privacy/offboard', expect.objectContaining({ preview: true }));
    expect(api.crmPost.mock.calls[0][1].password).toBeUndefined();
    const confirm = screen.getByRole('button', { name: 'Offboard' });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'pw-pw-pw-pw' } });
    fireEvent.click(confirm);
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(api.crmPost).toHaveBeenLastCalledWith('/api/privacy/offboard', {
      agent_id: 'leaving@b.test', successor_agent_id: 'owner@b.test', reason: 'left the brokerage',
      preview: false, password: 'pw-pw-pw-pw' });
  });

  it('says removing their Google connection is pending when Google has not confirmed it (audit 22.7)', async () => {
    const onDone = vi.fn();
    const result = { preview: false, state: 'running', external_revocation: 'pending',
      credential_revocation: [{ credential_id: 'c1', provider: 'google', status: 'revoke_failed' }] };
    api.crmPost.mockResolvedValueOnce(PLAN).mockResolvedValueOnce(result);
    render(<OffboardMemberForm member={MEMBERS[1]} members={MEMBERS} onDone={onDone} onCancel={() => {}} />);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'left the brokerage' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByTestId('offboard-plan');
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'pw-pw-pw-pw' } });
    fireEvent.click(screen.getByRole('button', { name: 'Offboard' }));
    const pending = await screen.findByTestId('offboard-pending');
    expect(pending.textContent).toMatch('Google has not yet confirmed it is removed');
    expect(pending.textContent).not.toMatch('access is gone');
    expect(onDone).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(onDone).toHaveBeenCalledWith(result);
  });

  it('does not blame Google when it is not a Google connection that is pending', async () => {
    const result = { preview: false, state: 'running', external_revocation: 'pending', credential_revocation: [] };
    api.crmPost.mockResolvedValueOnce(PLAN).mockResolvedValueOnce(result);
    render(<OffboardMemberForm member={MEMBERS[1]} members={MEMBERS} onDone={() => {}} onCancel={() => {}} />);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'left the brokerage' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByTestId('offboard-plan');
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'pw-pw-pw-pw' } });
    fireEvent.click(screen.getByRole('button', { name: 'Offboard' }));
    const pending = await screen.findByTestId('offboard-pending');
    expect(pending.textContent).toMatch('connected accounts are switched off');
    expect(pending.textContent).not.toMatch('Google');
  });

  it('closes at once when the revocation is confirmed', async () => {
    const onDone = vi.fn();
    api.crmPost.mockResolvedValueOnce(PLAN).mockResolvedValueOnce({ preview: false, external_revocation: 'complete' });
    render(<OffboardMemberForm member={MEMBERS[1]} members={MEMBERS} onDone={onDone} onCancel={() => {}} />);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'left the brokerage' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByTestId('offboard-plan');
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'pw-pw-pw-pw' } });
    fireEvent.click(screen.getByRole('button', { name: 'Offboard' }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(screen.queryByTestId('offboard-pending')).toBeNull();
  });

  it('says the number is parked when the successor already has one (audit 22.5)', async () => {
    api.crmPost.mockResolvedValueOnce({ ...PLAN, telephony: 'parked_successor_has_own_route' });
    render(<OffboardMemberForm member={MEMBERS[1]} members={MEMBERS} onDone={() => {}} onCancel={() => {}} />);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'left' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    const plan = await screen.findByTestId('offboard-plan');
    expect(plan.textContent).toMatch('stops taking calls and texts. It is not released');
  });

  it('offers only active colleagues as successors, never the person leaving', () => {
    render(<OffboardMemberForm member={MEMBERS[1]} members={MEMBERS} onDone={() => {}} onCancel={() => {}} />);
    const options = [...screen.getByLabelText(/Hand their open work/).options].map((o) => o.value);
    expect(options).toEqual(['owner@b.test']);
  });

  it('changing the successor discards a stale preview', async () => {
    const members = [...MEMBERS, { id: 'u4', agent_id: 'other@b.test', full_name: 'Other', role: 'agent', status: 'active' }];
    api.crmPost.mockResolvedValueOnce(PLAN);
    render(<OffboardMemberForm member={MEMBERS[1]} members={members} onDone={() => {}} onCancel={() => {}} />);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'left' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByTestId('offboard-plan');
    fireEvent.change(screen.getByLabelText(/Hand their open work/), { target: { value: 'other@b.test' } });
    expect(screen.queryByTestId('offboard-plan')).toBeNull();
    expect(screen.getByRole('button', { name: 'Preview' })).toBeTruthy();
  });

  it('with nobody to take over, says so instead of offering a form', () => {
    render(<OffboardMemberForm member={MEMBERS[1]} members={[MEMBERS[1]]} onDone={() => {}} onCancel={() => {}} />);
    expect(screen.getByText(/needs a new owner/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Preview' })).toBeNull();
  });
});
