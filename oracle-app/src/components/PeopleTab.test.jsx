// @vitest-environment jsdom
/**
 * The contact book's list markup and its empty state.
 *
 * The source-status list was nested inside a second <ul> (a merge artifact),
 * so screen readers heard an empty list wrapping a list and axe failed the
 * page as serious ("list"). The empty state pointed people at opportunities
 * while the New contact button sat right above it.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);
vi.mock('./ClientCrmTab', () => ({ default: () => null }));
const { default: PeopleTab } = await import('./PeopleTab');

afterEach(cleanup);

describe('PeopleTab contact book', () => {
  it('lists contain only list items, and the empty state names the real action', async () => {
    api.crmGet.mockResolvedValue({ contacts: [] });
    const { container } = render(<PeopleTab />);
    expect(await screen.findByText('No contacts yet')).toBeTruthy();
    for (const list of container.querySelectorAll('ul, ol')) {
      for (const child of list.children) expect(child.tagName).toBe('LI');
    }
    expect(screen.getByText(/with New contact/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'New contact' })).toBeTruthy();
  });

  it('searches every contact on the server, not only the loaded page (33.3)', async () => {
    const page = Array.from({ length: 100 }, (_, i) => ({ id: `c${i}`, full_name: `Loaded ${i}`, phone: `+1302555${String(i).padStart(4, '0')}` }));
    api.crmGet.mockImplementation((path) => {
      if (path.includes('q=Zed')) return Promise.resolve({ contacts: [{ id: 'c240', full_name: 'Zed Contact 240' }] });
      return Promise.resolve({ contacts: page, next_cursor: 'cur-2' });
    });
    render(<PeopleTab />);
    expect(await screen.findByText('100 contacts loaded — more below')).toBeTruthy();
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'Zed' } });
    expect(await screen.findByText('Zed Contact 240')).toBeTruthy();
    expect(api.crmGet).toHaveBeenCalledWith('/api/crm/contacts?q=Zed&limit=100');
    expect(screen.queryByText('No matches')).toBeNull();
  });

  it('keeps partial phone matches among loaded contacts beside the server results', async () => {
    const page = [{ id: 'a', full_name: 'Ana', phone: '+13025550142' }, { id: 'b', full_name: 'Bo', phone: '+13025550199' }];
    api.crmGet.mockImplementation((path) => Promise.resolve(path.includes('q=') ? { contacts: [] } : { contacts: page }));
    render(<PeopleTab />);
    await screen.findByText('2 contacts loaded');
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '0142' } });
    await screen.findByText(/Searched all your contacts/);
    expect(screen.getByText('Ana')).toBeTruthy();
    expect(screen.queryByText('Bo')).toBeNull();
  });

  it('loads the next page without duplicates and shows that more exist', async () => {
    api.crmGet.mockImplementation((path) => Promise.resolve(path.includes('cursor=cur-2')
      ? { contacts: [{ id: 'c1', full_name: 'Again One' }, { id: 'c3', full_name: 'Third' }], next_cursor: null }
      : { contacts: [{ id: 'c1', full_name: 'One' }, { id: 'c2', full_name: 'Two' }], next_cursor: 'cur-2' }));
    render(<PeopleTab />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more contacts' }));
    expect(await screen.findByText('Third')).toBeTruthy();
    expect(screen.queryByText('Again One')).toBeNull();
    expect(screen.getByText('3 contacts loaded')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Load more contacts' })).toBeNull();
  });

  it('shows only the newest search answer', async () => {
    let releaseSlow;
    api.crmGet.mockImplementation((path) => {
      if (path.includes('q=Sam')) return new Promise((resolve) => { releaseSlow = () => resolve({ contacts: [{ id: 's', full_name: 'Sam Old' }] }); });
      if (path.includes('q=Sal')) return Promise.resolve({ contacts: [{ id: 'l', full_name: 'Sally New' }] });
      return Promise.resolve({ contacts: [{ id: 'x', full_name: 'Xavier' }] });
    });
    render(<PeopleTab />);
    await screen.findByText('Xavier');
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'Sam' } });
    await waitFor(() => expect(releaseSlow).toBeTypeOf('function'));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'Sal' } });
    expect(await screen.findByText('Sally New')).toBeTruthy();
    releaseSlow();
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText('Sam Old')).toBeNull();
  });
});

