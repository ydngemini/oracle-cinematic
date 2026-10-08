// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
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
});
