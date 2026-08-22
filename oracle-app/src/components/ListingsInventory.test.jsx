// @vitest-environment jsdom
/**
 * The listings surface, and the two shapes of the API it must not smooth over.
 *
 * GET/POST /api/crm/listings had no caller anywhere in the frontend — a listing
 * could not be created through the product at all, while the assistant could
 * already anchor to one and `update_listing` was an allowlisted agent tool.
 *
 *  1. A seller is an existing client OR a new contact, never both. The server
 *     422s when both arrive, so the form has to make that unreachable rather
 *     than merely invalid.
 *  2. Beds/baths/sqft do not live on a listing — they ride a companion record
 *     the server creates only when at least one is supplied. A listing without
 *     them has no such record, and an empty specs line would read as a studio.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// fireEvent rather than user-event: @testing-library/user-event is not a
// dependency of this app, and these interactions are plain clicks and typing.
async function click(element) {
  await act(async () => { fireEvent.click(element); });
}

async function type(element, value) {
  await act(async () => { fireEvent.change(element, { target: { value } }); });
}

const crmGet = vi.fn();
const crmPost = vi.fn();
vi.mock('../state/useCrmApi', () => ({
  crmGet: (...args) => crmGet(...args),
  crmPost: (...args) => crmPost(...args),
  crmPut: vi.fn(),
}));

const { default: ListingsInventory } = await import('./ListingsInventory');

afterEach(cleanup);
beforeEach(() => {
  crmGet.mockReset();
  crmPost.mockReset();
  crmGet.mockResolvedValue({ listings: [] });
  crmPost.mockResolvedValue({ id: 'l1' });
});

function listing(overrides = {}) {
  return {
    id: 'l1', address: '15 Main St', price: 384500, status: 'active',
    beds: 3, baths: 2, sqft: 1800, cover_url: null,
    seller: { id: 'c1', full_name: 'Dana Reed' }, lead_id: 'ld1', ...overrides,
  };
}

describe('ListingsInventory', () => {
  it('states when a listing has no property record rather than showing an empty specs line', async () => {
    crmGet.mockResolvedValue({ listings: [listing({ beds: null, baths: null, sqft: null })] });
    render(<ListingsInventory />);

    await waitFor(() => expect(screen.getByText('15 Main St')).toBeTruthy());
    expect(screen.getByText(/no companion property record/i)).toBeTruthy();
  });

  it('shows specs when the companion record exists', async () => {
    crmGet.mockResolvedValue({ listings: [listing()] });
    render(<ListingsInventory />);

    await waitFor(() => expect(screen.getByText(/3 bd · 2 ba · 1,800 sqft/)).toBeTruthy());
    expect(screen.queryByText(/no companion property record/i)).toBeNull();
  });

  it('says a photo is missing instead of showing a stand-in image', async () => {
    crmGet.mockResolvedValue({ listings: [listing({ cover_url: null })] });
    render(<ListingsInventory />);

    await waitFor(() => expect(screen.getByText('No photo uploaded')).toBeTruthy());
  });

  it('never sends both a client id and an inline seller', async () => {
    render(<ListingsInventory />);
    await waitFor(() => expect(crmGet).toHaveBeenCalled());

    await click(screen.getByRole('button', { name: /new listing/i }));
    await type(screen.getByLabelText('Address'), '15 Main St');
    await click(screen.getByLabelText('Existing client'));
    await type(screen.getByLabelText('Client id'), 'c-123');
    // Switching mode must replace the seller shape, not add a second one.
    await click(screen.getByLabelText('New contact'));
    await type(screen.getByLabelText('Full name'), 'Dana Reed');
    await type(screen.getByLabelText('Email'), 'dana@example.test');
    await click(screen.getByRole('button', { name: /create listing/i }));

    await waitFor(() => expect(crmPost).toHaveBeenCalled());
    const [, payload] = crmPost.mock.calls[0];
    expect(payload.seller).toBeTruthy();
    expect(payload.seller_client_id).toBeUndefined();
  });

  it('omits the seller entirely when none is chosen', async () => {
    render(<ListingsInventory />);
    await waitFor(() => expect(crmGet).toHaveBeenCalled());

    await click(screen.getByRole('button', { name: /new listing/i }));
    await type(screen.getByLabelText('Address'), '15 Main St');
    await click(screen.getByRole('button', { name: /create listing/i }));

    await waitFor(() => expect(crmPost).toHaveBeenCalled());
    const [, payload] = crmPost.mock.calls[0];
    expect(payload.seller).toBeUndefined();
    expect(payload.seller_client_id).toBeUndefined();
    expect(payload.address).toBe('15 Main St');
  });

  it('omits blank specs rather than sending zeros', async () => {
    render(<ListingsInventory />);
    await waitFor(() => expect(crmGet).toHaveBeenCalled());

    await click(screen.getByRole('button', { name: /new listing/i }));
    await type(screen.getByLabelText('Address'), '15 Main St');
    await click(screen.getByRole('button', { name: /create listing/i }));

    const [, payload] = crmPost.mock.calls[0];
    for (const key of ['price', 'beds', 'baths', 'sqft']) {
      expect(payload[key]).toBeUndefined();
    }
  });

  it('distinguishes a failed load from an empty workspace', async () => {
    crmGet.mockRejectedValue(new Error('502 Bad Gateway'));
    render(<ListingsInventory />);

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
    expect(screen.getByText(/not a finding that you have no listings/i)).toBeTruthy();
    expect(screen.queryByText(/No listings in this workspace yet/i)).toBeNull();
  });

  it('reports an empty workspace only when the load succeeded', async () => {
    crmGet.mockResolvedValue({ listings: [] });
    render(<ListingsInventory />);

    await waitFor(() => expect(screen.getByText(/No listings in this workspace yet/i)).toBeTruthy());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('surfaces the server refusal instead of a generic failure', async () => {
    crmPost.mockRejectedValue(new Error('provide seller_client_id OR an inline seller, not both'));
    render(<ListingsInventory />);
    await waitFor(() => expect(crmGet).toHaveBeenCalled());

    await click(screen.getByRole('button', { name: /new listing/i }));
    await type(screen.getByLabelText('Address'), '15 Main St');
    await click(screen.getByRole('button', { name: /create listing/i }));

    await waitFor(() => expect(screen.getByText(/not both/i)).toBeTruthy());
  });
});
