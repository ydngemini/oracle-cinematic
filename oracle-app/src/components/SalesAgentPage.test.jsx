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
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
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
    await screen.findByRole('button', { name: /Base Person/ });

    const search = screen.getByLabelText('Search');
    fireEvent.change(search, { target: { value: 'a' } });
    await waitFor(() => expect(pending.a).toBeDefined());
    fireEvent.change(search, { target: { value: 'ab' } });
    await waitFor(() => expect(pending.ab).toBeDefined());

    // The newer request (q=ab) resolves first, then the older one (q=a) resolves last.
    await act(async () => { pending.ab.resolve({ items: [entry('new', 'Newer Match')] }); });
    await screen.findByRole('button', { name: /Newer Match/ });
    await act(async () => { pending.a.resolve({ items: [entry('old', 'Older Match')] }); });

    // The name appears in the queue list AND the heading, so assert by role:
    // nothing about the older query is shown anywhere.
    expect(screen.queryAllByText('Older Match')).toHaveLength(0);
    expect(screen.getByRole('button', { name: /Newer Match/ }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Newer Match' })).toBeTruthy();
  });

  it('a stale request that fails never shows an error or leaves the queue loading', async () => {
    const pending = {};
    api.crmGet.mockImplementation((url) => {
      const q = new URL(url, 'http://localhost').searchParams.get('q');
      if (!q) return Promise.resolve({ items: [entry('base', 'Base Person')] });
      pending[q] = deferred();
      return pending[q].promise;
    });

    render(<SalesAgentPage onNavigate={() => {}} />);
    await screen.findByRole('button', { name: /Base Person/ });
    const search = screen.getByLabelText('Search');
    fireEvent.change(search, { target: { value: 'a' } });
    await waitFor(() => expect(pending.a).toBeDefined());
    fireEvent.change(search, { target: { value: 'ab' } });
    await waitFor(() => expect(pending.ab).toBeDefined());

    await act(async () => { pending.ab.resolve({ items: [entry('new', 'Newer Match')] }); });
    await screen.findByRole('button', { name: /Newer Match/ });
    // The OLDER request now fails: the user's current results are not theirs to spoil.
    await act(async () => { pending.a.reject(new Error('network dropped')); });

    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('Loading the work queue…')).toBeNull();
    expect(screen.getByRole('button', { name: /Newer Match/ })).toBeTruthy();
  });

  it('an older request settling first does not end the newer search\'s loading state', async () => {
    const pending = {};
    api.crmGet.mockImplementation((url) => {
      const q = new URL(url, 'http://localhost').searchParams.get('q');
      if (!q) return Promise.resolve({ items: [entry('base', 'Base Person')] });
      pending[q] = deferred();
      return pending[q].promise;
    });

    render(<SalesAgentPage onNavigate={() => {}} />);
    await screen.findByRole('button', { name: /Base Person/ });
    const search = screen.getByLabelText('Search');
    fireEvent.change(search, { target: { value: 'a' } });
    await waitFor(() => expect(pending.a).toBeDefined());
    fireEvent.change(search, { target: { value: 'ab' } });
    await waitFor(() => expect(pending.ab).toBeDefined());

    // The OLDER request settles while the newer one is still in flight.
    await act(async () => { pending.a.resolve({ items: [entry('old', 'Older Match')] }); });
    expect(screen.getByText('Loading the work queue…')).toBeTruthy();
    expect(screen.queryAllByText('Older Match')).toHaveLength(0);

    await act(async () => { pending.ab.resolve({ items: [entry('new', 'Newer Match')] }); });
    await screen.findByRole('button', { name: /Newer Match/ });
    expect(screen.queryByText('Loading the work queue…')).toBeNull();
  });
});

