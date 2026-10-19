// @vitest-environment jsdom
/**
 * The record list says which search hits are only possible matches
 * (AC11.04, F11.04). A partial owner or address hit can rank first, and the
 * card is what the agent clicks before the workspace opens.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(cleanup);

const crmGet = vi.fn();
vi.mock('../state/useCrmApi', () => ({ crmGet: (...args) => crmGet(...args) }));
// The workspace's last props, so a test can play its onHouseUpdate.
const workspace = { props: null };
vi.mock('./HouseWorkspace', () => ({
  default: (props) => {
    workspace.props = props;
    return null;
  },
}));
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
    expect(exact.textContent).not.toMatch(/confirmed house/i);
  });

  it('flags a confirmed house separately and keeps the search claim (T11.03)', async () => {
    crmGet.mockResolvedValue({
      listings: [
        record('3', '9 Elm St', {
          status: 'possible_match',
          basis: 'owner_partial',
          verified: true,
          confirmation: { state: 'confirmed', head_id: 'h', decided_at: '2026-10-17T16:00:00+00:00' },
        }),
      ],
      total: 1,
    });

    render(<HouseSelection />);

    const card = await screen.findByRole('button', { name: /9 Elm St/ });
    expect(card.textContent).toMatch(/possible match/i);
    expect(card.textContent).toMatch(/confirmed house/i);
  });

  it('carries a decision made in the workspace to the selected record card', async () => {
    const identity = { status: 'exact_address', basis: 'address_exact', verified: false };
    crmGet.mockResolvedValue({ listings: [record('4', '4 Ash Ct', identity)], total: 1 });

    render(<HouseSelection />);

    const card = await screen.findByRole('button', { name: /4 Ash Ct/ });
    expect(card.textContent).not.toMatch(/confirmed house/i);
    act(() => workspace.props.onHouseUpdate({ identity: { ...identity, verified: true } }));

    expect(screen.getByRole('button', { name: /4 Ash Ct/ }).textContent).toMatch(/confirmed house/i);
    expect(workspace.props.house.identity.verified).toBe(true);
  });

  it('lands a decision that resolves after a switch on the house it was made for', async () => {
    // The decision form is inline, so the cards stay clickable while the POST
    // is in flight: the update arrives through the callback of the house it
    // was made for, after another house is selected.
    const identity = { status: 'exact_address', basis: 'address_exact', verified: false };
    crmGet.mockResolvedValue({
      listings: [record('5', '5 Birch Rd', identity), record('6', '6 Cedar Ln', identity)],
      total: 2,
    });

    render(<HouseSelection />);

    await screen.findByRole('button', { name: /5 Birch Rd/ });
    expect(workspace.props.house.id).toBe('5');
    const updateForFive = workspace.props.onHouseUpdate;
    fireEvent.click(screen.getByRole('button', { name: /6 Cedar Ln/ }));
    await waitFor(() => expect(workspace.props.house.id).toBe('6'));

    const confirmed = {
      ...identity,
      verified: true,
      confirmation: { state: 'confirmed', head_id: 'h5', decided_at: '2026-10-17T16:00:00+00:00' },
      confirmable: true,
    };
    act(() => updateForFive({ identity: confirmed }));

    // The selected house was never decided, and stays as the search sent it.
    expect(workspace.props.house.id).toBe('6');
    expect(workspace.props.house.identity).toEqual(identity);
    expect(screen.getByRole('button', { name: /6 Cedar Ln/ }).textContent).not.toMatch(/confirmed house/i);
    // The house it was made for carries it.
    expect(screen.getByRole('button', { name: /5 Birch Rd/ }).textContent).toMatch(/confirmed house/i);
    fireEvent.click(screen.getByRole('button', { name: /5 Birch Rd/ }));
    await waitFor(() => expect(workspace.props.house.id).toBe('5'));
    expect(workspace.props.house.identity).toEqual(confirmed);
  });
});
