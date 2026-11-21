// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn(), crmPatch: vi.fn(), crmDelete: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: ClientNotes } = await import('./ClientNotes');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const AI_NOTE = {
  id: 'n1', pinned: false, created_at: '2026-10-07T21:31:32Z',
  body: "Notes from Neoh's call (the caller agreed to notes):\n- Prefers afternoon showings",
};

describe('ClientNotes', () => {
  it("shows the AI's call notes when they arrive for this client, without a reopen", async () => {
    api.crmGet.mockResolvedValueOnce({ notes: [] }).mockResolvedValueOnce({ notes: [AI_NOTE] });
    render(<ClientNotes clientId="c1" />);
    await waitFor(() => expect(api.crmGet).toHaveBeenCalledTimes(1));

    act(() => { window.dispatchEvent(new CustomEvent('crm:client-changed', { detail: { clientId: 'other' } })); });
    expect(api.crmGet).toHaveBeenCalledTimes(1);

    act(() => { window.dispatchEvent(new CustomEvent('crm:client-changed', { detail: { clientId: 'c1' } })); });
    expect(await screen.findByText(/Prefers afternoon showings/)).toBeTruthy();
    expect(api.crmGet).toHaveBeenCalledTimes(2);
  });

  it("lets the agent approve a note the AI proposed from a call", async () => {
    const proposed = { ...AI_NOTE, id: 'n2', review_state: 'proposed' };
    api.crmGet.mockResolvedValueOnce({ notes: [proposed] });
    api.crmPost.mockResolvedValueOnce({ ...proposed, review_state: 'approved', reviewed_by: 'agent-b' });
    render(<ClientNotes clientId="c1" />);
    expect(await screen.findByText('Proposed by Neoh from a call')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /approve/i }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith('/api/crm/clients/c1/notes/n2/approve', {}));
    await waitFor(() => expect(screen.queryByText('Proposed by Neoh from a call')).toBeNull());
  });

  // Audit 34.4 / RUN-03: the AI's call note grows while the call goes on, so
  // an edit or an approval names the version the agent saw (409 otherwise).
  const CHANGED = 'This note changed while you were editing; reload to see the latest.';
  const conflict = Object.assign(new Error(CHANGED), { status: 409 });

  it('a stale edit is refused, the draft stays, and the latest version is shown', async () => {
    const v1 = { ...AI_NOTE, id: 'n3', updated_at: '2026-11-20T21:00:00.000001+00:00' };
    const v2 = { ...v1, body: `${v1.body}\n- Call back Friday`, updated_at: '2026-11-20T21:00:05.000002+00:00' };
    api.crmGet.mockResolvedValueOnce({ notes: [v1] }).mockResolvedValue({ notes: [v2] });
    api.crmPatch.mockRejectedValueOnce(conflict).mockResolvedValueOnce({ ...v2, body: 'My summary' });
    render(<ClientNotes clientId="c1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit note' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Edit note' }), { target: { value: 'My summary' } });
    // The call appends while the editor is open; the pane reloads underneath.
    act(() => { window.dispatchEvent(new CustomEvent('crm:client-changed', { detail: { clientId: 'c1' } })); });
    await waitFor(() => expect(api.crmGet).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.crmPatch).toHaveBeenCalledWith(
      '/api/crm/clients/c1/notes/n3', { body: 'My summary', expected_updated_at: v1.updated_at }));
    expect((await screen.findByRole('alert')).textContent).toBe(CHANGED);
    await waitFor(() => expect(api.crmGet).toHaveBeenCalledTimes(3));
    expect(screen.getByRole('textbox', { name: 'Edit note' }).value).toBe('My summary');
    expect(screen.getByLabelText('Latest saved version').textContent).toContain('- Call back Friday');

    // Saving now is against the version on screen.
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.crmPatch).toHaveBeenLastCalledWith(
      '/api/crm/clients/c1/notes/n3', { body: 'My summary', expected_updated_at: v2.updated_at }));
  });

  it('approves only the version on screen; a changed note is reloaded, not approved', async () => {
    const shown = { ...AI_NOTE, id: 'n4', review_state: 'proposed', updated_at: '2026-11-20T21:00:00.000001+00:00' };
    api.crmGet.mockResolvedValue({ notes: [shown] });
    api.crmPost.mockRejectedValueOnce(conflict);
    render(<ClientNotes clientId="c1" />);
    fireEvent.click(await screen.findByRole('button', { name: /approve/i }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith(
      '/api/crm/clients/c1/notes/n4/approve', { expected_updated_at: shown.updated_at }));
    expect((await screen.findByRole('alert')).textContent).toBe(CHANGED);
    await waitFor(() => expect(api.crmGet).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Proposed by Neoh from a call')).toBeTruthy();
  });
});
