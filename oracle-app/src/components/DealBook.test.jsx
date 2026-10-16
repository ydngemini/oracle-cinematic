// @vitest-environment jsdom
/**
 * The deal room says how much of the book it shows, and pages by cursor
 * (T16.05, L16-04). It used to load 100 deals and label them "{total} shown".
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const crmGet = vi.fn();
vi.mock('../state/useCrmApi', () => ({
  crmGet: (...a) => crmGet(...a),
  crmPost: vi.fn(),
  crmPatch: vi.fn(),
  crmPut: vi.fn(),
  crmDelete: vi.fn(),
  crmUpload: vi.fn(),
}));

const { default: DealBook } = await import('./DealBook');

function deal(i, status = 'active') {
  return { id: `d${i}`, status, version: 1, updated_at: new Date(Date.UTC(2026, 9, 16, 0, 0, 1000 - i)).toISOString(), property_address: `${i} Main St` };
}

beforeEach(() => {
  crmGet.mockReset();
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(0); return 1; });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('Deal room paging', () => {
  it('a partial list says "N of total" and loads the next page by cursor', async () => {
    crmGet.mockImplementation((url) => (url.includes('after=')
      ? Promise.resolve({ transactions: [deal(3), deal(2)], total: 4, next_cursor: null })
      : Promise.resolve({ transactions: [deal(1), deal(0, 'closed')], total: 4, next_cursor: 'abc=' })));
    render(<DealBook />);
    await screen.findByText('2 of 4 shown');
    expect(screen.getByText('1 active (of shown)')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Show more (2 more)' }));
    await screen.findByText('4 shown');
    expect(crmGet).toHaveBeenCalledWith('/api/portfolio/transactions?limit=100&after=abc%3D');
    expect(screen.queryByRole('button', { name: /show more/i })).toBeNull();
    expect(screen.getByText('3 active')).toBeTruthy();
  });

  it('a complete list is labelled as complete, with no Show more', async () => {
    crmGet.mockResolvedValue({ transactions: [deal(1)], total: 1, next_cursor: null });
    render(<DealBook />);
    await screen.findByText('1 shown');
    await waitFor(() => expect(screen.queryByText(/of shown/)).toBeNull());
    expect(screen.queryByRole('button', { name: /show more/i })).toBeNull();
  });
});
