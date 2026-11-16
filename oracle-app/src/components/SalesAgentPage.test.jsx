// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: SalesAgentPage } = await import('./SalesAgentPage');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function entry(id, name) {
  return {
    contact: { id, full_name: name, email: `${id}@example.test`, phone: '', preferred_channel: 'email' },
    client: { lead_score: 50, stage: 'lead' },
    reasons: ['recorded'],
    open_tasks: 0,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((res) => { resolve = res; });
  return { promise, resolve };
}

describe('SalesAgentPage work queue', () => {
  it('a stale work-queue response never overwrites the newer query\'s results', async () => {
    const pending = {};
    api.crmGet.mockImplementation((url) => {
      const q = new URL(url, 'http://localhost').searchParams.get('q');
      if (!q) return Promise.resolve({ items: [entry('base', 'Base Person')] });
      pending[q] = deferred();
      return pending[q].promise;
    });

    render(<SalesAgentPage onNavigate={() => {}} />);
    await screen.findByText('Base Person');

    const search = screen.getByLabelText('Search');
    fireEvent.change(search, { target: { value: 'a' } });
    await waitFor(() => expect(pending.a).toBeDefined());
    fireEvent.change(search, { target: { value: 'ab' } });
    await waitFor(() => expect(pending.ab).toBeDefined());

    // The newer request (q=ab) resolves first, then the older one (q=a) resolves last.
    await act(async () => { pending.ab.resolve({ items: [entry('new', 'Newer Match')] }); });
    await screen.findByText('Newer Match');
    await act(async () => { pending.a.resolve({ items: [entry('old', 'Older Match')] }); });

    expect(screen.queryByText('Older Match')).toBeNull();
    expect(screen.getByText('Newer Match')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Newer Match/ }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Newer Match' })).toBeTruthy();
  });
});
