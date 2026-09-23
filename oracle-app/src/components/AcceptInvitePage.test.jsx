// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock('../lib/apiClient', () => api);

const { AcceptInvitePage } = await import('./AcceptInvitePage');

const INVITE = {
  brokerage: 'Lockwood Realty', email: 'john@example.com', role: 'agent',
  invited_by: 'nat@lockwood.test', expires_at: '2026-10-01T00:00:00Z', state: 'pending',
};

function withToken(token) {
  window.history.replaceState({}, '', token ? `/accept-invite?token=${token}` : '/accept-invite');
}

beforeEach(() => {
  api.apiGet.mockReset();
  api.apiPost.mockReset();
  withToken('tok-123');
  api.apiGet.mockResolvedValue(INVITE);
});
afterEach(cleanup);

describe('AcceptInvitePage', () => {
  it('names the brokerage and who invited them', async () => {
    render(<AcceptInvitePage />);
    expect(await screen.findByText(/Join Lockwood Realty on Neoh/)).toBeTruthy();
    expect(screen.getByText(/nat@lockwood.test/)).toBeTruthy();
    expect(screen.getByText('john@example.com')).toBeTruthy();
  });

  it('looks the invitation up by the token in the URL', async () => {
    render(<AcceptInvitePage />);
    await waitFor(() => expect(api.apiGet).toHaveBeenCalledWith(
      '/auth/invitation?token=tok-123'));
  });

  it('refuses a link with no token instead of calling the API', async () => {
    withToken('');
    render(<AcceptInvitePage />);
    expect(await screen.findByText(/missing its token/)).toBeTruthy();
    expect(api.apiGet).not.toHaveBeenCalled();
  });

  it('never asks for an email, a brokerage or a tenant id', async () => {
    render(<AcceptInvitePage />);
    await screen.findByRole('heading', { name: /Join Lockwood Realty on Neoh/ });
    expect(screen.queryByLabelText(/email/i)).toBeNull();
    expect(screen.queryByLabelText(/brokerage/i)).toBeNull();
    expect(screen.queryByLabelText(/tenant/i)).toBeNull();
    expect(screen.getByLabelText('Your name')).toBeTruthy();
    expect(screen.getByLabelText('Choose a password')).toBeTruthy();
  });

  it('will not submit until a name and a long-enough password are given', async () => {
    render(<AcceptInvitePage />);
    await screen.findByRole('heading', { name: /Join Lockwood Realty on Neoh/ });
    const join = screen.getByRole('button', { name: /Join Lockwood Realty/ });
    expect(join.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'John' } });
    fireEvent.change(screen.getByLabelText('Choose a password'), { target: { value: 'short' } });
    expect(join.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Choose a password'),
      { target: { value: 'correct horse battery' } });
    expect(join.disabled).toBe(false);
  });

  it('posts only the token, password and name', async () => {
    api.apiPost.mockResolvedValue({ tenant_id: 't1', role: 'agent' });
    // Scoped, and restored below: replacing window.location outright leaks
    // into every later test in the file.
    const realLocation = window.location;
    const assign = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...realLocation, assign, search: '?token=tok-123', pathname: '/accept-invite' },
    });
    render(<AcceptInvitePage />);
    await screen.findByRole('heading', { name: /Join Lockwood Realty on Neoh/ });
    fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'John' } });
    fireEvent.change(screen.getByLabelText('Choose a password'),
      { target: { value: 'correct horse battery' } });
    fireEvent.click(screen.getByRole('button', { name: /Join Lockwood Realty/ }));
    await waitFor(() => expect(api.apiPost).toHaveBeenCalledWith('/auth/accept-invite', {
      token: 'tok-123', password: 'correct horse battery', full_name: 'John',
    }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/'));
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
  });

  it.each([
    ['accepted', /already been used/],
    ['revoked', /withdrew this invitation/],
    ['expired', /has expired/],
  ])('explains a %s invitation rather than showing a form', async (state, says) => {
    api.apiGet.mockResolvedValue({ ...INVITE, state });
    render(<AcceptInvitePage />);
    expect(await screen.findByText(says)).toBeTruthy();
    expect(screen.queryByLabelText('Choose a password')).toBeNull();
  });

  it('reports a rejected token without a form', async () => {
    api.apiGet.mockRejectedValue(new Error('This invitation link is not valid.'));
    render(<AcceptInvitePage />);
    expect(await screen.findByText('This invitation link is not valid.')).toBeTruthy();
    expect(screen.queryByLabelText('Choose a password')).toBeNull();
  });

  it('surfaces a server refusal on submit and lets them try again', async () => {
    api.apiPost.mockRejectedValue(new Error(
      'That email already belongs to a Neoh account in another brokerage.'));
    render(<AcceptInvitePage />);
    await screen.findByRole('heading', { name: /Join Lockwood Realty on Neoh/ });
    fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'John' } });
    fireEvent.change(screen.getByLabelText('Choose a password'),
      { target: { value: 'correct horse battery' } });
    fireEvent.click(screen.getByRole('button', { name: /Join Lockwood Realty/ }));
    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent', 'That email already belongs to a Neoh account in another brokerage.');
    // Not stuck in a spinner — they can correct and retry.
    await waitFor(() => expect(
      screen.getByRole('button', { name: /Join Lockwood Realty/ }).disabled).toBe(false));
  });

  it('shows an owner invitation as an owner invitation', async () => {
    api.apiGet.mockResolvedValue({ ...INVITE, role: 'broker_owner' });
    render(<AcceptInvitePage />);
    expect(await screen.findByText(/as an owner/)).toBeTruthy();
  });
});
