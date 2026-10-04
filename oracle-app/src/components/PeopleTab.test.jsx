// @vitest-environment jsdom
/**
 * The contact book's list markup and its empty state.
 *
 * The source-status list was nested inside a second <ul> (a merge artifact),
 * so screen readers heard an empty list wrapping a list and axe failed the
 * page as serious ("list"). The empty state pointed people at opportunities
 * while the New contact button sat right above it.
 */
import { cleanup, render, screen } from '@testing-library/react';
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
});
