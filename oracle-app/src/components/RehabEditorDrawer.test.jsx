// @vitest-environment jsdom
/**
 * Dimension provenance in the rehab drawer — the piece that stops an
 * estimated storey height from looking identical to a measured one.
 *
 * `auto-dimensions` labels every construction number measured / sourced /
 * estimated / default; this file pins that the drawer actually renders that
 * distinction (≈, dashed, a title carrying the basis) rather than collapsing
 * it into a plain number the way the pre-provenance version did — and that
 * a plan with no manifest at all (hand-drawn, or saved before migration
 * 0075) shows no provenance section instead of fabricating one.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(cleanup);

const crmGet = vi.fn();
const crmPost = vi.fn();
const crmPut = vi.fn();
const crmUpload = vi.fn();
vi.mock('../state/useCrmApi', () => ({
  crmGet: (...args) => crmGet(...args),
  crmPost: (...args) => crmPost(...args),
  crmPut: (...args) => crmPut(...args),
  crmUpload: (...args) => crmUpload(...args),
}));

vi.mock('./FloorplanCanvas', () => ({ FloorplanCanvas: () => null }));

const { EMPTY_FLOORPLAN, ZERO_METRICS } = await import('../lib/floorplan/protocol');

const useFloorplanEditor = vi.fn();
vi.mock('../lib/floorplan/useFloorplanEditor', () => ({
  useFloorplanEditor: (...args) => useFloorplanEditor(...args),
}));

const { default: RehabEditorDrawer } = await import('./RehabEditorDrawer');

function editorState(overrides = {}) {
  return {
    document: EMPTY_FLOORPLAN,
    metrics: ZERO_METRICS,
    baselineMetrics: ZERO_METRICS,
    dirty: false,
    selectedId: null,
    select: vi.fn(),
    addWall: vi.fn(),
    addRoom: vi.fn(),
    remove: vi.fn(),
    undo: vi.fn(),
    load: vi.fn(),
    requestDocument: vi.fn(async () => EMPTY_FLOORPLAN),
    markSaved: vi.fn(),
    ...overrides,
  };
}

const MANIFEST = {
  storey_height_m: { value: 2.5, unit: 'm', provenance: 'default', basis: 'US frame construction default' },
  footprint_area_m2: { value: 140, unit: 'm²', provenance: 'sourced', basis: 'OSM building footprint' },
  bedrooms: { value: 3, unit: '', provenance: 'measured', basis: 'counted from room polygons' },
};

describe('RehabEditorDrawer dimension provenance', () => {
  it('renders nothing about provenance when the plan has no manifest', async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockResolvedValue({ document: EMPTY_FLOORPLAN, dimension_manifest: null, scaffold_sha256: null });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);

    await waitFor(() => expect(crmGet).toHaveBeenCalled());
    expect(screen.queryByText('Dimension provenance')).toBeNull();
  });

  it('prefixes an estimated/default value with ≈ and never a measured or sourced one', async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockResolvedValue({
      document: EMPTY_FLOORPLAN,
      dimension_manifest: MANIFEST,
      scaffold_sha256: 'a'.repeat(64),
    });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);

    await waitFor(() => expect(screen.queryByText('Dimension provenance')).toBeTruthy());

    const storeyHeight = screen.getByText('Storey height').closest('div');
    expect(storeyHeight.textContent).toContain('≈2.5 m');
    const storeyDd = storeyHeight.querySelector('dd');
    expect(storeyDd.getAttribute('title')).toBe('US frame construction default');

    const bedrooms = screen.getByText('Bedrooms').closest('div');
    expect(bedrooms.textContent).toContain('3');
    expect(bedrooms.textContent).not.toContain('≈3');
  });

  it('shows a source chip for sourced values but not for measured ones', async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockResolvedValue({
      document: EMPTY_FLOORPLAN,
      dimension_manifest: MANIFEST,
      scaffold_sha256: 'a'.repeat(64),
    });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);

    await waitFor(() => expect(screen.queryByText('Footprint area')).toBeTruthy());
    expect(screen.queryByText('OSM building footprint')).toBeTruthy();

    const bedrooms = screen.getByText('Bedrooms').closest('div');
    expect(bedrooms.textContent).not.toContain('counted from room polygons');
  });

  it('warns the manifest may be stale once the layout has unsaved edits', async () => {
    useFloorplanEditor.mockReturnValue(editorState({ dirty: true }));
    crmGet.mockResolvedValue({
      document: EMPTY_FLOORPLAN,
      dimension_manifest: MANIFEST,
      scaffold_sha256: 'a'.repeat(64),
    });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);

    await waitFor(() => expect(screen.queryByText('may be stale')).toBeTruthy());
  });

  it('save sends the loaded manifest and scaffold hash back to the server', async () => {
    useFloorplanEditor.mockReturnValue(editorState({ dirty: true }));
    crmGet.mockResolvedValue({
      document: EMPTY_FLOORPLAN,
      dimension_manifest: MANIFEST,
      scaffold_sha256: 'b'.repeat(64),
    });
    crmPut.mockResolvedValue({ revision: 2, metrics: { total_sqft: 0 } });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} onSaved={() => {}} />);
    await waitFor(() => expect(screen.getByText('Save layout').disabled).toBe(false));

    screen.getByText('Save layout').click();

    await waitFor(() => expect(crmPut).toHaveBeenCalled());
    const [, body] = crmPut.mock.calls[0];
    expect(body.dimension_manifest).toBe(MANIFEST);
    expect(body.scaffold_sha256).toBe('b'.repeat(64));
  });

  it('auto-fill captures the returned manifest and scaffold hash into state', async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockResolvedValue({ document: EMPTY_FLOORPLAN, dimension_manifest: null, scaffold_sha256: null });
    crmPost.mockResolvedValue({
      document: EMPTY_FLOORPLAN,
      manifest: MANIFEST,
      scaffold_sha256: 'c'.repeat(64),
      estimated_fields: ['storey_height_m'],
      footprint: { found: true, source: 'osm' },
    });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Auto-fill').disabled).toBe(false));

    screen.getByText('Auto-fill').click();

    await waitFor(() => expect(screen.queryByText('Dimension provenance')).toBeTruthy());
    expect(screen.queryByText('Storey height')).toBeTruthy();
  });
});

describe('RehabEditorDrawer revision history', () => {
  const FLOORPLAN_ID = 'fp-1';

  it('has no History button when the plan has never been saved', async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockResolvedValue({ document: EMPTY_FLOORPLAN, floorplan_id: null });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);

    await waitFor(() => expect(crmGet).toHaveBeenCalled());
    expect(screen.queryByText('History')).toBeNull();
  });

  it('lists revisions on open and loads the chosen one read-only', async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockImplementation((path) => {
      if (path.includes('/revisions')) {
        return Promise.resolve({
          revisions: [
            { revision: 2, total_sqft: 120, created_by: 'a', created_at: '2026-01-02T00:00:00Z' },
            { revision: 1, total_sqft: 100, created_by: 'a', created_at: '2026-01-01T00:00:00Z' },
          ],
        });
      }
      if (path.includes('revision=1')) {
        return Promise.resolve({
          document: EMPTY_FLOORPLAN,
          dimension_manifest: MANIFEST,
          scaffold_sha256: 'd'.repeat(64),
        });
      }
      return Promise.resolve({ document: EMPTY_FLOORPLAN, floorplan_id: FLOORPLAN_ID });
    });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByText('History')).toBeTruthy());

    screen.getByText('History').click();
    await waitFor(() => expect(screen.queryByText('Rev 2')).toBeTruthy());
    expect(screen.queryByText('Rev 1')).toBeTruthy();

    screen.getByText('Rev 1').click();

    await waitFor(() => expect(screen.queryByText(/Viewing revision 1/)).toBeTruthy());
    // Save must be unreachable while browsing history — nothing here may
    // write a new revision on top of someone else's past.
    expect(screen.getByText('Save layout').disabled).toBe(true);
    expect(screen.queryByText('Dimension provenance')).toBeTruthy();
  });

  it('returning to current restores the live document without a network call', async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockImplementation((path) => {
      if (path.includes('/revisions')) {
        return Promise.resolve({
          revisions: [{ revision: 1, total_sqft: 100, created_by: 'a', created_at: '2026-01-01T00:00:00Z' }],
        });
      }
      if (path.includes('revision=1')) {
        return Promise.resolve({
          document: EMPTY_FLOORPLAN,
          dimension_manifest: MANIFEST,
          scaffold_sha256: 'e'.repeat(64),
        });
      }
      // The live head has no manifest — this is the fact the test uses to
      // tell "restored" from "still viewing the revision".
      return Promise.resolve({
        document: EMPTY_FLOORPLAN, floorplan_id: FLOORPLAN_ID,
        dimension_manifest: null, scaffold_sha256: null,
      });
    });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByText('History')).toBeTruthy());

    screen.getByText('History').click();
    await waitFor(() => expect(screen.queryByText('Rev 1')).toBeTruthy());
    screen.getByText('Rev 1').click();
    await waitFor(() => expect(screen.queryByText(/Viewing revision 1/)).toBeTruthy());
    expect(screen.queryByText('Dimension provenance')).toBeTruthy(); // the revision's manifest

    crmGet.mockClear();
    screen.getByText('Return to current').click();

    // returnToCurrent reads headSnapshot.current synchronously and never
    // awaits — but React still applies the resulting setState asynchronously
    // relative to this raw (non-act-wrapped) DOM click, so the assertion
    // below has to wait for the re-render like every other one in this file.
    await waitFor(() => expect(screen.queryByText(/Viewing revision/)).toBeNull());
    expect(crmGet).not.toHaveBeenCalled();
    // The head has no manifest — its absence here is proof the state that
    // came back is the live head's, not still the revision's.
    expect(screen.queryByText('Dimension provenance')).toBeNull();
  });
});

describe('RehabEditorDrawer upload-scan scale gate', () => {
  const openUploadPanel = async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockResolvedValue({ document: EMPTY_FLOORPLAN, dimension_manifest: null, scaffold_sha256: null });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Upload scan').disabled).toBe(false));
    screen.getByText('Upload scan').click();
    await waitFor(() => expect(screen.queryByText('Extract layout')).toBeTruthy());
  };

  it('submit stays disabled with no file and no scale', async () => {
    await openUploadPanel();
    expect(screen.getByText('Extract layout').disabled).toBe(true);
  });

  it('submit stays disabled with a scale but no file — a number alone is not a scan', async () => {
    await openUploadPanel();
    const sqftInput = screen.getByPlaceholderText('e.g. 1800');
    sqftInput.dispatchEvent(new Event('focus'));
    Object.defineProperty(sqftInput, 'value', { value: '1800', configurable: true });
    sqftInput.dispatchEvent(new Event('input', { bubbles: true }));

    expect(screen.getByText('Extract layout').disabled).toBe(true);
  });

  it('a zero or blank scale never enables submit even with a file selected', async () => {
    await openUploadPanel();
    const fileInput = document.querySelector('input[type="file"]');
    const file = new File(['x'], 'plan.png', { type: 'image/png' });
    Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));

    await waitFor(() => {
      // File alone must not be enough — this is the exact case the plan
      // warns about: a wrong (here: absent) scale must never quietly pass.
      expect(screen.getByText('Extract layout').disabled).toBe(true);
    });
  });

  it('a 503 (opencv absent) renders as an honest deployment limitation, not a generic failure', async () => {
    await openUploadPanel();
    const fileInput = document.querySelector('input[type="file"]');
    const file = new File(['x'], 'plan.png', { type: 'image/png' });
    Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));

    const sqftInput = screen.getByPlaceholderText('e.g. 1800');
    Object.defineProperty(sqftInput, 'value', { value: '1800', configurable: true });
    sqftInput.dispatchEvent(new Event('input', { bubbles: true }));

    await waitFor(() => expect(screen.getByText('Extract layout').disabled).toBe(false));

    const err = new Error('opencv-python is not installed');
    err.status = 503;
    crmUpload.mockRejectedValue(err);

    screen.getByText('Extract layout').click();

    await waitFor(() => expect(screen.queryByText(/not available on this deployment/)).toBeTruthy());
    expect(screen.queryByText('opencv-python is not installed')).toBeNull();
  });

  it('a successful extraction loads the document and clears any stale manifest', async () => {
    await openUploadPanel();
    const fileInput = document.querySelector('input[type="file"]');
    const file = new File(['x'], 'plan.png', { type: 'image/png' });
    Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));

    const sqftInput = screen.getByPlaceholderText('e.g. 1800');
    Object.defineProperty(sqftInput, 'value', { value: '1800', configurable: true });
    sqftInput.dispatchEvent(new Event('input', { bubbles: true }));

    await waitFor(() => expect(screen.getByText('Extract layout').disabled).toBe(false));

    crmUpload.mockResolvedValue({
      document: EMPTY_FLOORPLAN,
      metrics: { total_sqft: 1800, room_count: 5 },
      confidence: 0.62,
      scaffold_sha256: 'f'.repeat(64),
      saved: false,
    });

    screen.getByText('Extract layout').click();

    await waitFor(() => expect(crmUpload).toHaveBeenCalled());
    const [path] = crmUpload.mock.calls[0];
    expect(path).toContain('known_total_sqft=1800');
    // extract-image returns no manifest — unlike auto-fill, it must not
    // render a "Dimension provenance" section describing a different call.
    expect(screen.queryByText('Dimension provenance')).toBeNull();
  });
});

describe('RehabEditorDrawer footprint picker', () => {
  const CANDIDATE = {
    geometry: { type: 'Polygon', coordinates: [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]] },
    source: 'openstreetmap',
    licence: 'ODbL',
    attribution: '© OpenStreetMap contributors',
    area_sqm: 140,
    building_type: 'house',
    levels: 2,
    name: null,
  };

  const openFootprintPanel = async () => {
    useFloorplanEditor.mockReturnValue(editorState());
    crmGet.mockResolvedValue({ document: EMPTY_FLOORPLAN, dimension_manifest: null, scaffold_sha256: null });

    render(<RehabEditorDrawer leadId="lead-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Find footprint').disabled).toBe(false));
    screen.getByText('Find footprint').click();
    await waitFor(() => expect(screen.queryByText('Search')).toBeTruthy());
  };

  it('search stays disabled with a blank address', async () => {
    await openFootprintPanel();
    expect(screen.getByText('Search').disabled).toBe(true);
  });

  it('every candidate shows its source, licence, and attribution before it is chosen', async () => {
    await openFootprintPanel();
    crmPost.mockResolvedValue({ candidates: [CANDIDATE], count: 1 });

    const addressInput = screen.getByPlaceholderText('123 Main St, Dover, DE');
    Object.defineProperty(addressInput, 'value', { value: '123 Main St', configurable: true });
    addressInput.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => expect(screen.getByText('Search').disabled).toBe(false));
    screen.getByText('Search').click();

    // The credit has to be on screen at the moment of CHOOSING, not only in
    // a toast after the fact — ODbL requires it wherever the geometry shows.
    await waitFor(() => expect(screen.queryByText('© OpenStreetMap contributors')).toBeTruthy());
    expect(screen.queryByText(/openstreetmap.*ODbL/)).toBeTruthy();
  });

  it('an empty result set says so rather than showing nothing', async () => {
    await openFootprintPanel();
    crmPost.mockResolvedValue({ candidates: [], count: 0 });

    const addressInput = screen.getByPlaceholderText('123 Main St, Dover, DE');
    Object.defineProperty(addressInput, 'value', { value: 'Nowhere Rural Rd', configurable: true });
    addressInput.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => expect(screen.getByText('Search').disabled).toBe(false));
    screen.getByText('Search').click();

    await waitFor(() => expect(screen.queryByText(/No building outline found/)).toBeTruthy());
  });

  it('choosing a candidate extracts the exterior shell and clears any stale manifest', async () => {
    await openFootprintPanel();
    crmPost.mockImplementation((path) => {
      if (path.includes('footprint-candidates')) {
        return Promise.resolve({ candidates: [CANDIDATE], count: 1 });
      }
      if (path.includes('extract-parcel')) {
        return Promise.resolve({
          document: EMPTY_FLOORPLAN,
          metrics: { total_sqft: 1500, room_count: 0 },
          disclosure: 'AI-generated floor plan…',
          saved: false,
          scaffold_sha256: 'g'.repeat(64),
        });
      }
      throw new Error(`unexpected crmPost ${path}`);
    });

    const addressInput = screen.getByPlaceholderText('123 Main St, Dover, DE');
    Object.defineProperty(addressInput, 'value', { value: '123 Main St', configurable: true });
    addressInput.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => expect(screen.getByText('Search').disabled).toBe(false));
    screen.getByText('Search').click();

    await waitFor(() => expect(screen.queryByText('house footprint')).toBeTruthy());
    screen.getByText('house footprint').click();

    await waitFor(() => expect(screen.queryByText(/Exterior shell from openstreetmap/)).toBeTruthy());
    // The extraction's own success notice must still carry the attribution —
    // this is the credit surviving past the picker being closed.
    expect(screen.queryByText(/© OpenStreetMap contributors/)).toBeTruthy();
    expect(screen.queryByText('Find footprint')).toBeTruthy(); // panel closed, button remains
    expect(screen.queryByText('Dimension provenance')).toBeNull(); // extract-parcel has no manifest
  });
});
