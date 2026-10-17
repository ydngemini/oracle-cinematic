// @vitest-environment jsdom
/**
 * The homeowner's public-records list says which findings are only possible
 * matches (AC11.04, F11.04). A lien matched to the parcel by a fuzzy search is
 * a candidate, not a fact about the client's house, and this page is read by
 * the client. A `matched` status is only the submitter's claim, so it reads
 * "Not verified" until a reviewer confirms the finding, and a released lien
 * says it was released.
 */

import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./TourViewer', () => ({ default: () => null }));

const { default: SecureDossierPage } = await import('./SecureDossierPage');

function finding(overrides) {
  return {
    finding_type: 'lien',
    amount: 45000,
    recorded_at: '2024-01-02',
    released_at: null,
    match_status: 'possible_match',
    chain_gap: false,
    review_status: 'required',
    notes: null,
    warning: 'Preliminary public-record finding; not an insured title search.',
    ...overrides,
  };
}

function serve(titleSummary) {
  const dossier = {
    read_only: true,
    link_kind: 'seller',
    asset_scope: { summary: true, title_summary: true },
    watermark_text: 'CONFIDENTIAL',
    assets: {
      summary: { address: '1 Test Lane', state: 'DE' },
      title_summary: titleSummary,
    },
  };
  globalThis.fetch = vi.fn(async (url) => {
    const json = String(url).includes('/portal/session/')
      ? { session_token: 'session-1' }
      : dossier;
    return { ok: true, json: async () => json };
  });
}

beforeEach(() => {
  window.history.replaceState({}, '', '/vault/secure-access/tok-1');
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function findingRows() {
  const heading = await screen.findByRole('heading', { name: 'Public records' });
  return within(heading.closest('section')).getAllByRole('listitem');
}

describe('SecureDossierPage public records', () => {
  it('labels a possible match as one', async () => {
    serve([finding({ match_status: 'possible_match' })]);
    render(<SecureDossierPage />);

    const [row] = await findingRows();
    expect(row.textContent).toMatch(/\$45,000/);
    expect(within(row).getByText(/possible match/i)).toBeTruthy();
    // The label is its own word, not run into the finding type.
    expect(row.textContent).toMatch(/lien\s+Possible match/);
  });

  it('labels an unresolved match as a possible match and a matched record under review as not verified', async () => {
    serve([
      finding({ finding_type: 'judgment', match_status: 'unresolved' }),
      finding({ finding_type: 'mortgage', match_status: 'matched' }),
    ]);
    render(<SecureDossierPage />);

    const [unresolved, matched] = await findingRows();
    expect(unresolved.textContent).toMatch(/possible match/i);
    // `matched` is the submitter's claim; no person has confirmed it.
    expect(matched.textContent).not.toMatch(/possible match/i);
    expect(matched.textContent).toMatch(/mortgage\s+Not verified/);
    expect(screen.getByText(/no one has confirmed/i)).toBeTruthy();
    // Nothing on this page claims a finding was verified.
    expect(document.body.textContent).not.toMatch(/(?<!not )verified/i);
  });

  it('leaves a matched finding a reviewer confirmed unlabelled', async () => {
    serve([finding({ match_status: 'matched', review_status: 'confirmed' })]);
    render(<SecureDossierPage />);

    const [row] = await findingRows();
    expect(row.textContent).not.toMatch(/possible match|not verified/i);
    expect(screen.queryByText(/no one has confirmed/i)).toBeNull();
  });

  it('labels a finding with a missing or unknown match status as a possible match', async () => {
    serve([
      finding({ match_status: undefined }),
      finding({ match_status: null }),
      finding({ match_status: 'probable' }),
      // A reviewer's confirmation does not turn a possible match into a fact.
      finding({ match_status: 'possible_match', review_status: 'confirmed' }),
    ]);
    render(<SecureDossierPage />);

    const rows = await findingRows();
    expect(rows).toHaveLength(4);
    for (const row of rows) expect(row.textContent).toMatch(/possible match/i);
  });

  it('says a released lien was released', async () => {
    serve([
      finding({ match_status: 'released', released_at: '2023-05-01' }),
      finding({ match_status: 'matched', released_at: '2022-03-04' }),
      finding({ match_status: 'released', released_at: null }),
    ]);
    render(<SecureDossierPage />);

    const [released, matchedReleased, undated] = await findingRows();
    const day = (iso) => new Date(iso).toLocaleDateString(undefined, { timeZone: 'UTC' });
    expect(released.textContent).toContain(`Released ${day('2023-05-01')}`);
    expect(matchedReleased.textContent).toContain(`Released ${day('2022-03-04')}`);
    expect(undated.textContent).toMatch(/lien\s+Not verified\s+Released\$45,000/);
    // `released` claims the record is this house's; that is still unconfirmed.
    for (const row of [released, matchedReleased]) {
      expect(row.textContent).toMatch(/Not verified/);
    }
  });

  it('shows the release date as recorded, whatever the viewer\'s time zone', async () => {
    // released_at is a calendar date. West of UTC, a local rendering of its
    // UTC midnight is the day before: pin a zone where that would show.
    const env = globalThis.process.env;
    const previous = env.TZ;
    env.TZ = 'America/Los_Angeles';
    try {
      serve([finding({ match_status: 'released', released_at: '2023-05-01' })]);
      render(<SecureDossierPage />);
      const [row] = await findingRows();
      const recorded = new Date(Date.UTC(2023, 4, 1)).toLocaleDateString(undefined, { timeZone: 'UTC' });
      const dayBefore = new Date(Date.UTC(2023, 3, 30)).toLocaleDateString(undefined, { timeZone: 'UTC' });
      expect(row.textContent).toContain(`Released ${recorded}`);
      expect(row.textContent).not.toContain(dayBefore);
    } finally {
      if (previous === undefined) delete env.TZ; else env.TZ = previous;
    }
  });

  it('reads a claimed match as not verified when its review status is missing or unknown', async () => {
    // Fail closed: only an explicit 'confirmed' review drops the label.
    serve([
      finding({ match_status: 'matched', review_status: undefined }),
      finding({ match_status: 'matched', review_status: null }),
      finding({ match_status: 'released', review_status: 'approved-ish' }),
    ]);
    render(<SecureDossierPage />);
    for (const row of await findingRows()) {
      expect(row.textContent).toMatch(/Not verified/);
    }
  });

  it('explains a possible match only when one is listed', async () => {
    serve([finding({ match_status: 'matched' })]);
    render(<SecureDossierPage />);

    await findingRows();
    expect(screen.queryByText(/may belong to a different property/i)).toBeNull();
    cleanup();

    serve([finding({ match_status: 'possible_match' })]);
    render(<SecureDossierPage />);
    await findingRows();
    expect(screen.getByText(/may belong to a different property/i)).toBeTruthy();
  });
});
