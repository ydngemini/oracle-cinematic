// @vitest-environment jsdom
/**
 * The screen that made intelligence authoring possible, and the three refusals
 * that keep its output a finding rather than a claim.
 *
 * Every POST on /api/intelligence requires at least one `source_record_id`
 * resolvable against the immutable `source_records` table, and nothing listed
 * that table — so thirteen routes were unreachable through the product. What is
 * pinned here is not that the form renders, but that it cannot produce a score
 * nothing supports:
 *
 *  1. The citation object goes back to the server byte-for-byte. SourceCitation
 *     sets extra="forbid", so one added field is a 422 — a client that rebuilt
 *     the object would break the moment either model changed.
 *  2. A source whose licence forbids property-level use is shown and disabled,
 *     not hidden. "Why can I not cite this?" needs an answer on the screen.
 *  3. A blank signal is never sent as a zero. The engine weights only what it
 *     is given and reports coverage; an invented zero raises confidence in a
 *     score no observation backs.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

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
}));

const { default: IntelligenceAuthoring } = await import('./IntelligenceAuthoring');

const CITE = {
  source_record_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  source: 'New Castle County Assessor',
  record_id: 'NCC-2026-000142',
  source_url: 'https://data.newcastlede.gov/parcels',
  observed_at: '2026-08-01',
  retrieved_at: '2026-08-02T09:30:00Z',
  license: 'municipal-open-data',
  evidence_status: 'observed',
};

function source(overrides = {}) {
  return {
    cite: { ...CITE, ...(overrides.cite || {}) },
    source_key: 'de-newcastle-assessor',
    property_key: 'DE-NCC-0142',
    jurisdiction: 'DE',
    property_level_allowed: true,
    payload_purged: false,
    expires_at: null,
    ...overrides,
  };
}

function mockApi({ citable = [source()] } = {}) {
  crmGet.mockImplementation((path) => {
    if (path.startsWith('/api/intelligence/sources')) {
      return Promise.resolve({ citable, count: citable.length });
    }
    return Promise.resolve({
      distress_signals: [
        { signal: 'tax_delinquency', weight: 0.2 },
        { signal: 'vacancy', weight: 0.14 },
      ],
    });
  });
}

afterEach(cleanup);
beforeEach(() => {
  crmGet.mockReset();
  crmPost.mockReset();
  crmPost.mockResolvedValue({ id: 'i1' });
  mockApi();
});

async function renderPanel(props = {}) {
  await act(async () => {
    render(<IntelligenceAuthoring propertyKey="DE-NCC-0142" {...props} />);
  });
  await waitFor(() => expect(crmGet).toHaveBeenCalled());
}

it('posts the citation exactly as the listing returned it', async () => {
  const onAuthored = vi.fn();
  await renderPanel({ onAuthored });

  await click(screen.getByRole('checkbox'));
  await type(screen.getByLabelText(/tax delinquency/i), '0.8');
  await click(screen.getByRole('button', { name: /score against/i }));

  await waitFor(() => expect(crmPost).toHaveBeenCalled());
  const [path, body] = crmPost.mock.calls[0];
  expect(path).toBe('/api/intelligence/pre-distress');
  expect(body.property_key).toBe('DE-NCC-0142');
  // Byte-for-byte: extra="forbid" makes any added key a 422.
  expect(body.sources).toEqual([CITE]);
  expect(body.signals).toEqual({ tax_delinquency: 0.8 });
  expect(onAuthored).toHaveBeenCalled();
});

it('never sends a blank signal as a zero', async () => {
  await renderPanel();

  await click(screen.getByRole('checkbox'));
  await type(screen.getByLabelText(/tax delinquency/i), '0.8');
  // `vacancy` is left blank — the engine must weight only what was observed.
  await click(screen.getByRole('button', { name: /score against/i }));

  await waitFor(() => expect(crmPost).toHaveBeenCalled());
  expect(crmPost.mock.calls[0][1].signals).not.toHaveProperty('vacancy');
});

it('shows a licence-blocked source disabled rather than hiding it', async () => {
  mockApi({ citable: [source({ property_level_allowed: false })] });
  await renderPanel();

  expect(screen.getByRole('checkbox').disabled).toBe(true);
  expect(screen.getByText(/licence forbids property-level use/i)).toBeTruthy();
});

it('marks a purged record as still citable provenance', async () => {
  mockApi({ citable: [source({ payload_purged: true })] });
  await renderPanel();

  expect(screen.getByText(/payload purged \(provenance kept\)/i)).toBeTruthy();
  expect(screen.getByRole('checkbox').disabled).toBe(false);
});

it('will not score with evidence but no signal, or a signal but no evidence', async () => {
  await renderPanel();
  const button = screen.getByRole('button', { name: /score against/i });

  expect(button.disabled).toBe(true);

  await click(screen.getByRole('checkbox'));
  expect(button.disabled).toBe(true);          // evidence, no signal

  await click(screen.getByRole('checkbox'));
  await type(screen.getByLabelText(/tax delinquency/i), '0.8');
  expect(button.disabled).toBe(true);          // signal, no evidence

  await click(screen.getByRole('checkbox'));
  expect(button.disabled).toBe(false);
  expect(crmPost).not.toHaveBeenCalled();
});

it('explains an empty evidence list as a data-credential problem, not a broken screen', async () => {
  mockApi({ citable: [] });
  await renderPanel();

  expect(screen.getByText(/no public-record observations have been retained/i)).toBeTruthy();
  expect(screen.getByText(/credential has usually lapsed/i)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /score against/i })).toBeNull();
});

it('translates the licence refusal into the reason, not the error code', async () => {
  await renderPanel();
  crmPost.mockRejectedValueOnce(
    Object.assign(new Error('SOURCE_LICENSE_FORBIDS_PROPERTY_USE'), {
      code: 'SOURCE_LICENSE_FORBIDS_PROPERTY_USE',
      status: 422,
    }),
  );

  await click(screen.getByRole('checkbox'));
  await type(screen.getByLabelText(/tax delinquency/i), '0.8');
  await click(screen.getByRole('button', { name: /score against/i }));

  await waitFor(() => {
    expect(screen.getByRole('alert').textContent).toMatch(/forbids property-level use/i);
  });
});
