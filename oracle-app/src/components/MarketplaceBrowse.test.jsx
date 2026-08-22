// @vitest-environment jsdom
/**
 * The marketplace disposition surface, and the three claims it must not
 * overstate.
 *
 * Eight backend routes had no UI at all, so nothing was enforcing these in the
 * client:
 *
 *  1. Nothing here sends. `send_state` goes not_sent → approved_not_sent, and
 *     an approved draft is still an undelivered one — the UI has to keep
 *     saying so after approval, not fall silent and imply completion.
 *  2. The competition-claim guard (422 when a message alleges competing offers
 *     the record cannot support) must surface the server's own reason, not a
 *     generic "failed" that reads as a transient error worth retrying.
 *  3. A 404 is the feature gate, not a breakage — "not enabled here" and
 *     "something went wrong" are different facts.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(cleanup);

const crmGet = vi.fn();
const crmPost = vi.fn();
const crmPut = vi.fn();
vi.mock('../state/useCrmApi', () => ({
  crmGet: (...args) => crmGet(...args),
  crmPost: (...args) => crmPost(...args),
  crmPut: (...args) => crmPut(...args),
}));

const { default: MarketplaceBrowse } = await import('./MarketplaceBrowse');

const PUBLICATION = {
  id: 'pub-1',
  state: 'published',
  asking_price: 210000,
  truthful_summary: {
    address: '15 Main St, Dover, DE',
    state: 'DE',
    arv: 300000,
    rehab: 45000,
    beds: 3,
    baths: 2,
  },
};

async function renderWithPublication() {
  crmGet.mockResolvedValue({ publications: [PUBLICATION] });
  render(<MarketplaceBrowse />);
  await waitFor(() => expect(screen.queryByText('15 Main St, Dover, DE')).toBeTruthy());
  screen.getByText('15 Main St, Dover, DE').click();
  await waitFor(() => expect(screen.queryByText('Bidding message')).toBeTruthy());
}

describe('MarketplaceBrowse loading and empty states', () => {
  it('an empty marketplace explains why rather than showing a bare void', async () => {
    crmGet.mockResolvedValue({ publications: [] });
    render(<MarketplaceBrowse />);

    await waitFor(() => expect(screen.queryByText('No published properties.')).toBeTruthy());
    // The reason matters: nothing lands here until a contract is signed.
    expect(screen.queryByText(/signed assignment or seller\s+contract/)).toBeTruthy();
  });

  it('a 404 reads as "not enabled here", not as a failure', async () => {
    const err = new Error('Feature is not enabled for this deployment.');
    err.status = 404;
    crmGet.mockRejectedValue(err);

    render(<MarketplaceBrowse />);

    await waitFor(() => expect(screen.queryByText(/not enabled for this deployment/)).toBeTruthy());
  });

  it('a non-404 failure is reported as a failure', async () => {
    const err = new Error('Memory Core offline.');
    err.status = 503;
    crmGet.mockRejectedValue(err);

    render(<MarketplaceBrowse />);

    await waitFor(() => expect(screen.queryByText('Memory Core offline.')).toBeTruthy());
    expect(screen.queryByText(/not enabled/)).toBeNull();
  });
});

describe('MarketplaceBrowse buyer matching', () => {
  it('renders each match with the trace that explains its score', async () => {
    await renderWithPublication();
    crmPost.mockResolvedValue({
      publication_id: 'pub-1',
      matches: [{
        id: 'm-1',
        match_score: 0.87,
        verification_status: 'funds_verified',
        acquisition_history_verified: true,
        criteria_trace: { price_in_range: true, state_match: true, min_beds: false },
      }],
    });

    screen.getByText('Run match').click();

    await waitFor(() => expect(screen.queryByText('0.87')).toBeTruthy());
    expect(screen.queryByText('Funds verified')).toBeTruthy();
    expect(screen.queryByText('History verified')).toBeTruthy();
    // A bare score invites being trusted; the trace is what makes it auditable.
    expect(screen.queryByText('price in range')).toBeTruthy();
    expect(screen.queryByText('not met')).toBeTruthy();
  });

  it('an unverified buyer is labelled unverified, never upgraded', async () => {
    await renderWithPublication();
    crmPost.mockResolvedValue({
      matches: [{
        id: 'm-2',
        match_score: 0.4,
        verification_status: 'unverified',
        acquisition_history_verified: false,
        criteria_trace: {},
      }],
    });

    screen.getByText('Run match').click();

    await waitFor(() => expect(screen.queryByText('Unverified')).toBeTruthy());
    expect(screen.queryByText('History verified')).toBeNull();
  });

  it('no matches says so instead of leaving the section blank', async () => {
    await renderWithPublication();
    crmPost.mockResolvedValue({ matches: [] });

    screen.getByText('Run match').click();

    await waitFor(() => expect(screen.queryByText(/No active buyer requests matched/)).toBeTruthy());
  });
});

describe('MarketplaceBrowse bidding message — nothing here sends', () => {
  const typeDraft = (text) => {
    const textarea = screen.getByPlaceholderText('Message to matched buyers…');
    Object.defineProperty(textarea, 'value', { value: text, configurable: true });
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  };

  it('states up front that approving does not deliver anything', async () => {
    await renderWithPublication();
    expect(screen.queryByText(/approving does not\s+deliver anything/)).toBeTruthy();
  });

  it('drafting reports not_sent and approving still reports not sent', async () => {
    await renderWithPublication();
    typeDraft('This property is available for assignment.');
    await waitFor(() => expect(screen.getByText('Draft for approval').disabled).toBe(false));

    crmPost.mockResolvedValueOnce({
      draft: {}, approval: { id: 'appr-1' }, send_state: 'not_sent',
    });
    screen.getByText('Draft for approval').click();

    await waitFor(() => expect(screen.queryByText(/Drafted — not sent/)).toBeTruthy());

    crmPost.mockResolvedValueOnce({
      approval: { id: 'appr-1' }, send_state: 'approved_not_sent',
    });
    screen.getByText('Approve draft').click();

    // The critical assertion: after approval the UI must still say NOT SENT.
    await waitFor(() => expect(screen.queryByText(/Approved — not sent/)).toBeTruthy());
    expect(screen.queryByText(/approval-bound email or SMS command/)).toBeTruthy();
  });

  it('a refused competition claim surfaces the server reason verbatim', async () => {
    await renderWithPublication();
    typeDraft('There are multiple offers on this property.');
    await waitFor(() => expect(screen.getByText('Draft for approval').disabled).toBe(false));

    const err = new Error('Competition claim is unsupported by at least two recorded offers.');
    err.status = 422;
    crmPost.mockRejectedValueOnce(err);

    screen.getByText('Draft for approval').click();

    // The agent has to learn it was the CLAIM that was refused — not that the
    // request broke and is worth retrying unchanged.
    await waitFor(() => expect(
      screen.queryByText('Competition claim is unsupported by at least two recorded offers.'),
    ).toBeTruthy());
    // And no approval button appears for a draft that was never created.
    expect(screen.queryByText('Approve draft')).toBeNull();
  });

  it('an empty draft cannot be submitted', async () => {
    await renderWithPublication();
    expect(screen.getByText('Draft for approval').disabled).toBe(true);
  });
});

describe('MarketplaceBrowse — My listings (publication authoring)', () => {
  const SIGNED_CONTRACT = {
    id: 'doc-1',
    title: 'Assignment — 15 Main St',
    document_type: 'assignment',
    status: 'signed',
    lead_id: 'lead-1',
  };

  const routeMock = ({ publications = [], documents = [] }) => (path) => {
    if (path.includes('/marketplace/publications')) return Promise.resolve({ publications });
    if (path.includes('/contracts/documents')) return Promise.resolve({ documents });
    return Promise.resolve({ publications: [] });
  };

  const openListings = async (mock) => {
    crmGet.mockImplementation(mock);
    render(<MarketplaceBrowse />);
    screen.getByText('My listings').click();
    await waitFor(() => expect(screen.queryByText('Your publications')).toBeTruthy());
  };

  it('offers only signed assignment/seller contracts linked to a property', async () => {
    await openListings(routeMock({
      documents: [
        SIGNED_CONTRACT,
        { id: 'doc-2', title: 'Unsigned', document_type: 'assignment', status: 'draft', lead_id: 'lead-2' },
        { id: 'doc-3', title: 'No lead', document_type: 'assignment', status: 'signed', lead_id: null },
        { id: 'doc-4', title: 'Wrong type', document_type: 'disclosure', status: 'signed', lead_id: 'lead-4' },
      ],
    }));

    await waitFor(() => expect(screen.queryByText('Assignment — 15 Main St')).toBeTruthy());
    // Each of these would 409 server-side; offering the button would be a lie.
    expect(screen.queryByText('Unsigned')).toBeNull();
    expect(screen.queryByText('No lead')).toBeNull();
    expect(screen.queryByText('Wrong type')).toBeNull();
  });

  it('a contract already published is not offered again', async () => {
    await openListings(routeMock({
      publications: [{ id: 'pub-1', state: 'published', contract_document_id: 'doc-1', truthful_summary: {} }],
      documents: [SIGNED_CONTRACT],
    }));

    await waitFor(() => expect(screen.queryByText('No eligible signed contracts.')).toBeTruthy());
  });

  it('a draft is shown as a draft and says it is not yet visible to others', async () => {
    await openListings(routeMock({
      documents: [],
      publications: [{
        id: 'pub-1', state: 'draft', asking_price: 210000,
        truthful_summary: { address: '15 Main St', state: 'DE' },
      }],
    }));

    await waitFor(() => expect(screen.queryByText('Draft')).toBeTruthy());
    // The publish affordance only exists for drafts.
    expect(screen.queryByText('Publish')).toBeTruthy();
  });

  it('publish stays disabled until the audit reason meets the server minimum', async () => {
    await openListings(routeMock({
      documents: [],
      publications: [{
        id: 'pub-1', state: 'draft', truthful_summary: { address: '15 Main St' },
      }],
    }));

    await waitFor(() => expect(screen.queryByText('Publish')).toBeTruthy());
    expect(screen.getByText('Publish').disabled).toBe(true);

    const reason = screen.getByPlaceholderText('Reason for the audit trail (min 8 chars)');
    Object.defineProperty(reason, 'value', { value: 'short', configurable: true });
    reason.dispatchEvent(new Event('input', { bubbles: true }));
    // decide_approval requires >= 8 chars; a shorter one would 422 after the
    // click, so the gate belongs here too.
    await waitFor(() => expect(screen.getByText('Publish').disabled).toBe(true));

    Object.defineProperty(reason, 'value', { value: 'Reviewed and approved', configurable: true });
    reason.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => expect(screen.getByText('Publish').disabled).toBe(false));
  });

  it('a published publication offers no publish control', async () => {
    await openListings(routeMock({
      documents: [],
      publications: [{
        id: 'pub-1', state: 'published', truthful_summary: { address: '15 Main St' },
      }],
    }));

    await waitFor(() => expect(screen.queryByText('Published')).toBeTruthy());
    expect(screen.queryByText('Publish')).toBeNull();
  });
});

describe('MarketplaceBrowse — Buyers', () => {
  const PROFILE = {
    id: 'prof-1',
    client_name: 'Acme Capital',
    states: ['DE'],
    min_price: 100000,
    max_price: 300000,
    verification_status: 'unverified',
    active_request_count: 0,
  };

  const routeMock = ({ profiles = [], clients = [] }) => (path) => {
    if (path.includes('/buyers/profiles')) return Promise.resolve({ profiles });
    if (path.includes('/crm/clients')) return Promise.resolve({ clients });
    return Promise.resolve({});
  };

  const openBuyers = async (mock) => {
    crmGet.mockImplementation(mock);
    render(<MarketplaceBrowse />);
    screen.getByText('Buyers').click();
    await waitFor(() => expect(screen.queryByText('Buyer profiles')).toBeTruthy());
  };

  it('a profile with no active request says it is not eligible for matching', async () => {
    await openBuyers(routeMock({ profiles: [PROFILE] }));

    await waitFor(() => expect(screen.queryByText('Acme Capital')).toBeTruthy());
    // "Matched nothing" and "was never in the running" are different answers.
    expect(screen.queryByText(/No active request — not eligible for matching/)).toBeTruthy();
  });

  it('a profile with active requests reports the count instead', async () => {
    await openBuyers(routeMock({
      profiles: [{ ...PROFILE, active_request_count: 2 }],
    }));

    await waitFor(() => expect(screen.queryByText('2 active requests')).toBeTruthy());
    expect(screen.queryByText(/not eligible for matching/)).toBeNull();
  });

  it('only buyer-type clients are offered for a profile', async () => {
    await openBuyers(routeMock({
      clients: [
        { id: 'c-1', full_name: 'Buyer Co', client_type: 'buyer' },
        { id: 'c-2', full_name: 'Both Co', client_type: 'both' },
        { id: 'c-3', full_name: 'Seller Co', client_type: 'seller' },
      ],
    }));

    await waitFor(() => expect(screen.queryByText('Buyer Co')).toBeTruthy());
    expect(screen.queryByText('Both Co')).toBeTruthy();
    // The server 409s a non-buyer client; offering it would be a dead option.
    expect(screen.queryByText('Seller Co')).toBeNull();
  });

  it('says so plainly when there are no buyer-type clients to attach to', async () => {
    await openBuyers(routeMock({ clients: [] }));

    await waitFor(() => expect(
      screen.queryByText(/only attach to a client marked\s+as a buyer/),
    ).toBeTruthy());
  });

  it('saving a profile never asserts verification the buyer has not earned', async () => {
    await openBuyers(routeMock({
      clients: [{ id: 'c-1', full_name: 'Buyer Co', client_type: 'buyer' }],
    }));
    await waitFor(() => expect(screen.queryByText('Buyer Co')).toBeTruthy());

    const select = document.querySelector('select');
    Object.defineProperty(select, 'value', { value: 'c-1', configurable: true });
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await waitFor(() => expect(screen.getByText('Save profile').disabled).toBe(false));

    crmPut.mockResolvedValue({ id: 'prof-1' });
    screen.getByText('Save profile').click();

    await waitFor(() => expect(crmPut).toHaveBeenCalled());
    const [, body] = crmPut.mock.calls[0];
    expect(body.verification_status).toBe('unverified');
    expect(body.acquisition_history_verified).toBe(false);
  });
});
