// @vitest-environment jsdom
/**
 * The panel that closes the first-value gap.
 *
 * Onboarding asks a new broker which ZIPs they work and saves them; nothing read
 * them back, so a broker answered a wizard about their market and then faced an
 * app with zero leads, zero clients and zero listings.
 *
 * The two things pinned here both caused a silent blank panel in development:
 * the profile row is WRAPPED (`{"profile": {...}}`), and the field holds
 * free-text city names alongside ZIPs — only a ZIP can be handed to the
 * public-records search.
 */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const crmGet = vi.fn();
vi.mock('../state/useCrmApi', () => ({ crmGet: (...a) => crmGet(...a) }));

const { default: MarketSnapshot } = await import('./MarketSnapshot');

function api({ markets = ['19709'], listings, total = 26558 } = {}) {
  crmGet.mockImplementation((path) => {
    if (path === '/api/crm/profile') {
      return Promise.resolve({ profile: { target_markets: markets } });
    }
    return Promise.resolve({
      total,
      listings: listings ?? [
        { id: '1', address: '654 RT 425', owner_name: 'ELLERS PROPERTIES LLC', price: 180057 },
        { id: '2', address: '0 ROCKY HEIGHTS DR', owner_name: 'GENTRY ROY WESLEY', price: 128729 },
      ],
    });
  });
}

afterEach(cleanup);
beforeEach(() => { crmGet.mockReset(); api(); });

async function renderPanel() {
  await act(async () => { render(<MarketSnapshot />); });
  await waitFor(() => expect(crmGet).toHaveBeenCalled());
}

it('shows the public record for the broker\'s own ZIP', async () => {
  await renderPanel();

  await waitFor(() => expect(screen.getByText(/26,558/)).toBeTruthy());
  expect(screen.getByText(/654 RT 425/)).toBeTruthy();
  // Owner and assessed value are the two fields this catalog actually has.
  expect(screen.getByText(/ELLERS PROPERTIES LLC/)).toBeTruthy();
  expect(screen.getByText(/\$180,057/)).toBeTruthy();
});

it('reads the wrapped profile envelope, not the top level', async () => {
  // GET /api/crm/profile returns {"profile": {...}}. Reading target_markets off
  // the envelope yields undefined, which is indistinguishable from "no markets
  // set" — the panel renders its empty state and nobody knows why.
  crmGet.mockImplementation((path) =>
    path === '/api/crm/profile'
      ? Promise.resolve({ profile: { target_markets: ['19709'] }, target_markets: undefined })
      : Promise.resolve({ total: 5, listings: [] }));

  await renderPanel();

  await waitFor(() => expect(screen.getByText(/19709/)).toBeTruthy());
  expect(screen.queryByText(/No target ZIP codes/i)).toBeNull();
});

it('ignores non-ZIP markets rather than searching for them', async () => {
  // The same field holds city names typed elsewhere. `zip=Wilmington` returns an
  // empty list that reads as "no properties here" instead of "not a ZIP".
  api({ markets: ['Wilmington', '19709', 'DE'] });

  await renderPanel();

  await waitFor(() => expect(screen.getByText(/19709/)).toBeTruthy());
  const searched = crmGet.mock.calls.map(([p]) => p).filter((p) => p.includes('public-records'));
  expect(searched).toEqual(['/api/mls/public-records?zip=19709']);
});

it('tells a broker with no markets how to get some', async () => {
  api({ markets: [] });
  await renderPanel();

  await waitFor(() => expect(screen.getByText(/No target ZIP codes/i)).toBeTruthy());
  expect(screen.getByText(/My Profile/)).toBeTruthy();
});

it('a ZIP that fails does not blank the others', async () => {
  crmGet.mockImplementation((path) => {
    if (path === '/api/crm/profile') {
      return Promise.resolve({ profile: { target_markets: ['19709', '19901'] } });
    }
    if (path.includes('19709')) return Promise.reject(new Error('upstream'));
    return Promise.resolve({ total: 42, listings: [] });
  });

  await renderPanel();

  await waitFor(() => expect(screen.getByText(/could not be read just now/i)).toBeTruthy());
  expect(screen.getByText(/42/)).toBeTruthy();
});

it('says where the numbers come from', async () => {
  await renderPanel();
  await waitFor(() => expect(screen.getByText(/county public records, not an estimate/i)).toBeTruthy());
});
