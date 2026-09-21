// @vitest-environment jsdom
/**
 * The market overview reads the flat `StateMarketOverview` the backend actually
 * returns — `median_sale_price`, `median_days_on_market`, `months_of_supply`,
 * `active_listings`, `closed_sales_last_30d`, `avg_price_per_sqft`,
 * `yoy_price_change_pct`, plus `as_of_date` / `is_stale` / `data_vintage_days`.
 *
 * A prior version expected a `{ overview, top_counties }` envelope that no
 * endpoint ever sent, so every tile rendered a dash and the county table never
 * appeared. These tests pin the real contract.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const crmGet = vi.fn();
vi.mock('../state/useCrmApi', () => ({ crmGet: (...a) => crmGet(...a) }));

let primaryState = 'DE';
vi.mock('../state/StateContext', () => ({ useStateCtx: () => ({ primaryState }) }));

const { MarketDataPanels } = await import('./MarketDataPanels');

afterEach(cleanup);
beforeEach(() => {
  crmGet.mockReset();
  primaryState = 'DE';
});

const OVERVIEW = {
  state_code: 'DE',
  state_name: 'Delaware',
  median_sale_price: 384500,
  median_list_price: 399000,
  median_days_on_market: 33,
  months_of_supply: 2.4,
  active_listings: 1728,
  closed_sales_last_30d: 612,
  avg_price_per_sqft: 241,
  yoy_price_change_pct: 4.2,
  as_of_date: '2026-05-31',
  data_vintage_days: 84,
  is_stale: false,
};

it('renders the flat overview fields the backend sends', async () => {
  crmGet.mockResolvedValue(OVERVIEW);
  render(<MarketDataPanels />);

  await waitFor(() => expect(screen.getByText('$384,500')).toBeTruthy());
  expect(screen.getByText('Median Sale Price')).toBeTruthy();
  expect(screen.getByText('33')).toBeTruthy();            // days on market
  expect(screen.getByText('1,728')).toBeTruthy();         // active listings
  expect(screen.getByText('2.4')).toBeTruthy();           // months of supply
  expect(screen.getByText('612')).toBeTruthy();           // sold last 30d
  expect(screen.getByText(/2026-05-31 reporting period/)).toBeTruthy();
  expect(crmGet).toHaveBeenCalledWith('/api/market/DE/overview');
});

it('shows a dash, not a zero, for a deliberately-NULL column', async () => {
  crmGet.mockResolvedValue({ ...OVERVIEW, avg_price_per_sqft: null });
  render(<MarketDataPanels />);

  await waitFor(() => expect(screen.getByText('Price / sq ft')).toBeTruthy());
  const tile = screen.getByText('Price / sq ft').closest('div');
  expect(tile.textContent).toContain('—');
});

it('reads a 404 as "no data loaded for this state", not a broken service', async () => {
  crmGet.mockRejectedValue({ status: 404 });
  render(<MarketDataPanels />);

  await waitFor(() => expect(screen.getByText(/No market data has been loaded for DE/)).toBeTruthy());
});

it('falls back to the list price when no sale price is reported', async () => {
  crmGet.mockResolvedValue({ ...OVERVIEW, median_sale_price: null });
  render(<MarketDataPanels />);

  await waitFor(() => expect(screen.getByText('Median List Price')).toBeTruthy());
  expect(screen.getByText('$399,000')).toBeTruthy();
});

it('prompts for a state when none is selected', async () => {
  primaryState = null;
  render(<MarketDataPanels />);
  expect(screen.getByText('Select a state to view market data')).toBeTruthy();
  expect(crmGet).not.toHaveBeenCalled();
});
