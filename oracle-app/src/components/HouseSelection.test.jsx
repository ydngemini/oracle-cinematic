// @vitest-environment jsdom
/**
 * The record list says which search hits are only possible matches
 * (AC11.04, F11.04). A partial owner or address hit can rank first, and the
 * card is what the agent clicks before the workspace opens.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(cleanup);

const crmGet = vi.fn();
vi.mock('../state/useCrmApi', () => ({ crmGet: (...args) => crmGet(...args) }));
vi.mock('./HouseWorkspace', () => ({ default: () => null }));
vi.mock('./motion/AdaptiveViewTransition', () => ({
  AdaptiveViewTransition: ({ children }) => children,
}));

const { default: HouseSelection } = await import('./HouseSelection');

function record(id, address, identity) {
  return { id, address, state: 'DE', parcel_id: `P-${id}`, identity };
}

describe('HouseSelection record cards', () => {
  it('flags a partial hit as a possible match and leaves exact hits unflagged', async () => {
    crmGet.mockResolvedValue({
      listings: [
        record('1', '15 Main St', { status: 'possible_match', basis: 'address_partial', verified: false }),
        record('2', '200 Oak Ave', { status: 'exact_address', basis: 'address_exact', verified: false }),
      ],
      total: 2,
    });

    render(<HouseSelection />);

    const partial = await screen.findByRole('button', { name: /15 Main St/ });
    expect(partial.textContent).toMatch(/possible match/i);
    const exact = screen.getByRole('button', { name: /200 Oak Ave/ });
    expect(exact.textContent).not.toMatch(/possible match/i);
  });
});
