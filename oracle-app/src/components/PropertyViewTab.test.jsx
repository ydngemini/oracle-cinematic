// @vitest-environment jsdom
/**
 * The tour affordance on Property View.
 *
 * The failure this covers: Property View is where a capture is STARTED
 * (CaptureSessionPanel), and it had no way to show the result. The tour
 * resolver, the viewer and the honest badge all existed; nothing on this page
 * reached them. To a user looking for "the 3D tour", a surface that renders
 * nothing is indistinguishable from a feature that does not exist — which is
 * exactly how it was read.
 *
 * So both halves are pinned here: the walk is offered when there is one, and
 * the reason is stated when there is not.
 */

import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// vitest.config.js does not set `globals: true`, so Testing Library never
// registers its automatic cleanup — without this, renders accumulate in
// document.body and queries match across tests.
afterEach(cleanup);

const crmGet = vi.fn();
const crmPost = vi.fn();
const crmDelete = vi.fn();
const crmUpload = vi.fn();
vi.mock('../state/useCrmApi', () => ({
  crmGet: (...a) => crmGet(...a),
  crmPost: (...a) => crmPost(...a),
  crmDelete: (...a) => crmDelete(...a),
  crmUpload: (...a) => crmUpload(...a),
}));

const useTour = vi.fn();
vi.mock('../state/useTour', () => ({ useTour: (...a) => useTour(...a) }));

// Not under test here, and CaptureSessionPanel does its own fetching.
vi.mock('./CaptureSessionPanel', () => ({ default: () => null }));
// The viewer needs WebGL; what this page hands it is what is under test.
vi.mock('./TourViewer', async () => {
  const { createElement } = await import('react');
  return {
    default: ({ title, splatLabel, splatCaveats }) => createElement('div', {
      'data-testid': 'tour-viewer',
      'data-title': title,
      'data-label': splatLabel ?? '',
      'data-caveats': (splatCaveats || []).join('|'),
    }),
  };
});

const { default: PropertyViewTab } = await import('./PropertyViewTab');

const LEAD_ID = '33333333-3333-4333-8333-333333333333';
const EXACT = { status: 'exact_address', basis: 'address_exact', verified: false };
const POSSIBLE = { status: 'possible_match', basis: 'address_contains', verified: false };

/**
 * Drive the real lookup flow so the test exercises the wiring rather than a
 * mock of it: geocode → enrich + resolve → single exact match auto-selects → view.
 * The resolver's ILIKE only returns records whose address contains the typed
 * text, so the record carries the whole typed address, labelled as the
 * server labels a word-for-word match in the named state.
 */
function routeGet(url) {
  if (url.startsWith('/api/geocode')) {
    return Promise.resolve({ lat: 39.1, lng: -75.5, display_name: '1 Main St, Dover, DE 19901' });
  }
  if (url.startsWith('/api/enrich-property')) return Promise.resolve(null);
  if (url.startsWith('/api/crm/property-view/resolve')) {
    return Promise.resolve({
      leads: [{ id: LEAD_ID, address: '1 Main St, Dover, DE 19901', identity: EXACT }],
      listings: [],
    });
  }
  if (url.startsWith('/api/crm/property-view?')) {
    return Promise.resolve({ by_surface: {}, media: [], upload_links: [] });
  }
  return Promise.resolve(null);
}

async function lookUpAProperty() {
  render(<PropertyViewTab />);
  fireEvent.change(screen.getByLabelText(/property address/i), {
    target: { value: '1 Main St, Dover, DE 19901' },
  });
  fireEvent.submit(screen.getByLabelText(/property address/i).closest('form'));
  // The tour section only exists once a subject is resolved.
  await waitFor(() => expect(screen.getByRole('region', { name: /3d tour/i })).toBeTruthy());
}

beforeEach(() => {
  vi.clearAllMocks();
  crmGet.mockImplementation(routeGet);
});

describe('Property View offers the tour it can produce', () => {
  it('asks the resolver for the subject it resolved', async () => {
    useTour.mockReturnValue({ tour: null });
    await lookUpAProperty();
    await waitFor(() =>
      expect(useTour).toHaveBeenCalledWith(expect.objectContaining({ leadId: LEAD_ID })),
    );
  });

  it('offers a walk when this property has been captured', async () => {
    useTour.mockReturnValue({
      tour: { splat_url: '/public/splats/a.splat', is_this_property: true, pano_scene_count: 0 },
    });
    await lookUpAProperty();
    expect(screen.getByRole('button', { name: /step inside/i })).toBeTruthy();
  });

  it('hands the viewer the quality label and caveats the server gave (T12.10)', async () => {
    useTour.mockReturnValue({
      tour: {
        splat_url: '/public/splats/a.splat', is_this_property: true, pano_scene_count: 0,
        splat_label: 'Partial 3D walkthrough',
        splat_caveats: ["Parts of the capture couldn't be placed — some areas may be missing"],
      },
    });
    await lookUpAProperty();
    fireEvent.click(screen.getByRole('button', { name: /step inside/i }));
    const viewer = await screen.findByTestId('tour-viewer');
    expect(viewer.dataset.label).toBe('Partial 3D walkthrough');
    expect(viewer.dataset.caveats).toMatch(/couldn't be placed/);
  });

  it('says a generated space is not this home', async () => {
    useTour.mockReturnValue({
      tour: { splat_url: '/public/splats/demo.splat', is_this_property: false, pano_scene_count: 0 },
    });
    await lookUpAProperty();
    expect(screen.getByRole('button', { name: /not this home/i })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /step inside/i })).toBeNull();
    // The caveat must also appear as text, not only inside the button label —
    // the button scrolls away, the claim should not.
    expect(screen.getByText(/stand-in, not a capture of this address/i)).toBeTruthy();
  });
});

describe('Property View states absence instead of rendering nothing', () => {
  it('explains that no tour exists yet rather than showing an empty panel', async () => {
    useTour.mockReturnValue({ tour: { photo_count: 4, pano_scene_count: 0 } });
    await lookUpAProperty();
    // The regression: this region used to not exist at all.
    expect(screen.getByRole('region', { name: /3d tour/i })).toBeTruthy();
    expect(screen.getByText(/no 3d tour yet/i)).toBeTruthy();
    expect(screen.getByText(/4 photos on file/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /step inside/i })).toBeNull();
  });

  it('does not report "no tour" when the resolver simply did not answer', async () => {
    useTour.mockReturnValue({ tour: null });
    await lookUpAProperty();
    expect(screen.getByText(/could not reach the resolver/i)).toBeTruthy();
    expect(screen.queryByText(/no 3d tour yet/i)).toBeNull();
  });

  it('will not call a single 360 a walkthrough', async () => {
    useTour.mockReturnValue({ tour: { pano_scene_count: 1, photo_count: 0 } });
    await lookUpAProperty();
    expect(screen.queryByRole('button', { name: /step inside/i })).toBeNull();
    expect(screen.getByText(/at least two/i)).toBeTruthy();
  });
});


describe('360° capture mode — the no-GPU route to a walkable tour', () => {
  /** The bug: the server has accepted `capture=pano` since the pano work landed
   *  and writes property_pano_scenes, but the UI never sent the field, so every
   *  upload defaulted to "auto" and tier 2 was unreachable without a GPU. */
  async function pickPano(files) {
    useTour.mockReturnValue({ tour: { pano_scene_count: 0, photo_count: 0 } });
    await lookUpAProperty();
    fireEvent.click(screen.getByRole('radio', { name: /360/i }));
    const input = document.querySelector('input[type="file"]');
    fireEvent.change(input, { target: { files } });
    return input;
  }

  function img(name = 'scene.jpg', type = 'image/jpeg') {
    return new File(['x'], name, { type });
  }

  it('sends capture=pano so the server takes the 360 path', async () => {
    crmUpload.mockResolvedValue({ media: [{ id: 'm1' }] });
    await pickPano([img()]);
    await waitFor(() => expect(crmUpload).toHaveBeenCalled());
    const form = crmUpload.mock.calls[0][1];
    expect(form.get('capture')).toBe('pano');
  });

  it('defaults to auto so ordinary photo uploads are unchanged', async () => {
    crmUpload.mockResolvedValue({ media: [] });
    useTour.mockReturnValue({ tour: { pano_scene_count: 0, photo_count: 0 } });
    await lookUpAProperty();
    fireEvent.change(document.querySelector('input[type="file"]'), {
      target: { files: [img('front.jpg')] },
    });
    await waitFor(() => expect(crmUpload).toHaveBeenCalled());
    expect(crmUpload.mock.calls[0][1].get('capture')).toBe('auto');
  });

  it('sends the floor so scenes group by storey in the viewer', async () => {
    crmUpload.mockResolvedValue({ media: [] });
    await pickPano([img()]);
    await waitFor(() => expect(crmUpload).toHaveBeenCalled());
    expect(crmUpload.mock.calls[0][1].get('floor_index')).toBe('0');
  });

  it('refuses a video named as a 360 instead of letting the server reject it', async () => {
    crmUpload.mockClear();
    await pickPano([new File(['x'], 'walkthrough.mp4', { type: 'video/mp4' })]);
    expect(crmUpload).not.toHaveBeenCalled();
    expect(screen.getByText(/walkthrough\.mp4 is a video/i)).toBeTruthy();
  });

  it('states the 2:1 requirement rather than letting the upload fail blind', async () => {
    await pickPano([]);
    expect(screen.getByText(/2:1/)).toBeTruthy();
  });

  it('will not call one scene a walkable tour', async () => {
    useTour.mockReturnValue({ tour: { pano_scene_count: 1, photo_count: 0 } });
    await lookUpAProperty();
    fireEvent.click(screen.getByRole('radio', { name: /360/i }));
    expect(screen.getByText(/one more makes it walkable/i)).toBeTruthy();
  });

  it('confirms a walkable tour once two scenes exist', async () => {
    useTour.mockReturnValue({ tour: { pano_scene_count: 2, photo_count: 0 } });
    await lookUpAProperty();
    fireEvent.click(screen.getByRole('radio', { name: /360/i }));
    expect(screen.getByText(/2 scenes .* walkable 360/i)).toBeTruthy();
  });
});

describe('a large capture upload survives a dropped file (Neoh Space §45)', () => {
  it('uploads file by file, keeps what landed, names what failed, and retries only that', async () => {
    useTour.mockReturnValue({ tour: { pano_scene_count: 0, photo_count: 0 } });
    await lookUpAProperty();
    const files = ['a.jpg', 'b.jpg', 'c.jpg'].map((n) => new File(['x'], n, { type: 'image/jpeg' }));
    crmUpload
      .mockResolvedValueOnce({ media: [{ id: '1' }] })
      .mockRejectedValueOnce(new Error('Network connection lost'))
      .mockResolvedValueOnce({ media: [{ id: '3' }] });
    fireEvent.change(document.querySelector('input[type="file"]'), { target: { files } });

    await screen.findByText(/2 of 3 uploaded/);
    expect(crmUpload).toHaveBeenCalledTimes(3);
    // One file per request, in capture order.
    expect(crmUpload.mock.calls.map((c) => c[1].getAll('files')[0].name)).toEqual(['a.jpg', 'b.jpg', 'c.jpg']);
    const failed = screen.getByRole('group', { name: /files that did not upload/i });
    expect(failed.textContent).toMatch(/b\.jpg/);
    expect(failed.textContent).toMatch(/Network connection lost/);

    crmUpload.mockResolvedValueOnce({ media: [{ id: '2' }] });
    fireEvent.click(screen.getByRole('button', { name: /retry 1 failed file/i }));
    await screen.findByText(/1 file uploaded/);
    expect(crmUpload).toHaveBeenCalledTimes(4);
    expect(crmUpload.mock.calls[3][1].getAll('files')[0].name).toBe('b.jpg');
    expect(screen.queryByRole('group', { name: /files that did not upload/i })).toBeNull();
  });
});

describe('Property View never shows a failed source as "none" (A02)', () => {
  it('says "Couldn\'t check" for sources that timed out or failed', async () => {
    useTour.mockReturnValue({ tour: null });
    crmGet.mockImplementation((url) => {
      if (url.startsWith('/api/enrich-property')) {
        return Promise.resolve({
          pois: { radius_m: 1000, total_pois: null, error: 'upstream_timeout', lookup_status: 'timed_out' },
          flood: null,
          walkscore: null,
          source_status: {
            pois: { status: 'timed_out', reason: 'upstream_timeout', required: true },
            flood: { status: 'unavailable', reason: 'upstream_error', required: true },
            walkscore: { status: 'unavailable', reason: 'not_configured', required: false },
          },
          degraded: true,
        });
      }
      return routeGet(url);
    });
    await lookUpAProperty();
    const facts = document.querySelector('dl');
    expect(facts.textContent).toMatch(/Flood zone\s*Couldn't check/);
    expect(facts.textContent).toMatch(/Nearby POIs\s*Couldn't check/);
    expect(facts.textContent).toMatch(/Walk Score\s*Not configured/);
    expect(facts.textContent).not.toMatch(/POIs\s*0/);
    expect(screen.getByText(/couldn.t be checked just now/i)).toBeTruthy();
  });

  it('shows verified answers as answers', async () => {
    useTour.mockReturnValue({ tour: null });
    crmGet.mockImplementation((url) => {
      if (url.startsWith('/api/enrich-property')) {
        return Promise.resolve({
          pois: { radius_m: 1000, total_pois: 12, lookup_status: 'checked' },
          flood: { zone: 'UNKNOWN', mapped: false, lookup_status: 'checked' },
          walkscore: { walkscore: 71, lookup_status: 'checked' },
          source_status: {
            pois: { status: 'checked' }, flood: { status: 'checked' }, walkscore: { status: 'checked' },
          },
          degraded: false,
        });
      }
      return routeGet(url);
    });
    await lookUpAProperty();
    const facts = document.querySelector('dl');
    expect(facts.textContent).toMatch(/Flood zone\s*Not mapped by FEMA/);
    expect(facts.textContent).toMatch(/Walk Score\s*71/);
    expect(facts.textContent).toMatch(/Nearby POIs\s*12/);
    expect(facts.textContent).toMatch(/Est\. value\s*Not checked/);
    expect(screen.queryByText(/couldn.t be checked just now/i)).toBeNull();
  });

  it('a geocoder outage is not "address not found"', async () => {
    useTour.mockReturnValue({ tour: null });
    crmGet.mockImplementation((url) => {
      if (url.startsWith('/api/geocode')) {
        const err = new Error('geocoder unavailable; the address was not checked');
        err.status = 502;
        return Promise.reject(err);
      }
      return routeGet(url);
    });
    render(<PropertyViewTab />);
    fireEvent.change(screen.getByLabelText(/property address/i), { target: { value: '1 Main St, Dover, DE' } });
    fireEvent.submit(screen.getByLabelText(/property address/i).closest('form'));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/was not checked/);
    expect(alert.textContent).not.toMatch(/could not be located/);
  });
});

describe('Property View upload control', () => {
  it('keeps the hidden file input out of the tab order; the visible button opens it', async () => {
    useTour.mockReturnValue({ tour: null });
    await lookUpAProperty();
    const input = document.querySelector('input[type="file"]');
    expect(input).toBeTruthy();
    expect(input.tabIndex).toBe(-1);
    expect(screen.getByRole('button', { name: /^upload .*photos or video/i })).toBeTruthy();
  });
});

describe('Capture Studio launches from Property View (T12.03, L12-03)', () => {
  it('opens the camera studio, which falls back to the file picker without a camera', async () => {
    useTour.mockReturnValue({ tour: { pano_scene_count: 0, photo_count: 0 } });
    await lookUpAProperty();
    fireEvent.click(screen.getByRole('button', { name: /capture with camera/i }));
    const studio = await screen.findByRole('region', { name: 'Capture with camera' });
    expect(studio).toBeTruthy();
    // jsdom has no camera: the studio offers the existing picker instead.
    expect(screen.getByRole('alert').textContent).toMatch(/camera/i);
    expect(screen.getAllByRole('button', { name: 'Choose files instead' }).length).toBeGreaterThan(0);
  });

  it('is not offered for 360° scenes, which come from a 360 camera', async () => {
    useTour.mockReturnValue({ tour: { pano_scene_count: 0, photo_count: 0 } });
    await lookUpAProperty();
    fireEvent.click(screen.getByRole('radio', { name: /360/i }));
    expect(screen.queryByRole('button', { name: /capture with camera/i })).toBeNull();
  });
});

describe('Property View never attaches media to a possible match (T11.03, F11.04)', () => {
  /** The bug: the resolver's ILIKE '%12 Main St%' also finds "112 Main St", and
   *  a single hit was selected as the record every upload attaches to. */
  function resolveWith(answer) {
    crmGet.mockImplementation((url) => {
      if (url.startsWith('/api/crm/property-view/resolve')) return answer();
      return routeGet(url);
    });
  }

  async function lookUp(typed) {
    render(<PropertyViewTab />);
    fireEvent.change(screen.getByLabelText(/property address/i), { target: { value: typed } });
    fireEvent.submit(screen.getByLabelText(/property address/i).closest('form'));
    return screen.findByRole('heading', { name: /attach media to a record/i });
  }

  const subjectWasSelected = () => useTour.mock.calls.some(([arg]) => arg?.leadId === LEAD_ID);

  it('lists a substring hit as a possible match to confirm instead of selecting it', async () => {
    useTour.mockReturnValue({ tour: null });
    resolveWith(() => Promise.resolve({
      leads: [{ id: LEAD_ID, address: '112 Main St, Dover, DE 19901', identity: POSSIBLE }], listings: [],
    }));
    await lookUp('12 Main St, Dover, DE 19901');
    expect(subjectWasSelected()).toBe(false);
    expect(screen.queryByRole('region', { name: /3d tour/i })).toBeNull();
    const candidate = screen.getByRole('button', { name: /112 Main St/ });
    expect(candidate.textContent).toMatch(/possible match/i);
    expect(screen.getByText(/may be another property/i)).toBeTruthy();

    // Confirming it is the agent's choice, and then it is the subject.
    fireEvent.click(candidate);
    await waitFor(() => expect(subjectWasSelected()).toBe(true));
  });

  it('treats a candidate the server did not label as a possible match', async () => {
    useTour.mockReturnValue({ tour: null });
    resolveWith(() => Promise.resolve({ leads: [{ id: LEAD_ID, address: '112 Main St' }], listings: [] }));
    await lookUp('12 Main St, Dover, DE 19901');
    expect(subjectWasSelected()).toBe(false);
    expect(screen.getByRole('button', { name: /112 Main St/ }).textContent).toMatch(/possible match/i);
  });

  it('labels an exact address as not verified when it is one of several', async () => {
    useTour.mockReturnValue({ tour: null });
    resolveWith(() => Promise.resolve({
      leads: [
        { id: LEAD_ID, address: '1 Main St, Dover, DE 19901', identity: EXACT },
        { id: 'L2', address: '11 Main St, Dover, DE 19901', identity: POSSIBLE },
      ],
      listings: [],
    }));
    await lookUp('1 Main St, Dover, DE 19901');
    expect(subjectWasSelected()).toBe(false);
    expect(screen.getByRole('button', { name: /^Lead\s*1 Main St/ }).textContent).toMatch(/address match · not verified/i);
    expect(screen.getByRole('button', { name: /11 Main St/ }).textContent).toMatch(/possible match/i);
  });

  it('does not report "no existing record" when the record check failed', async () => {
    useTour.mockReturnValue({ tour: null });
    resolveWith(() => Promise.reject(new Error('timeout')));
    await lookUp('12 Main St, Dover, DE 19901');
    expect(screen.queryByText(/no existing record matches/i)).toBeNull();
    expect(screen.getByText(/couldn.t be checked/i)).toBeTruthy();
  });

  it('does not select a reused record that is only a possible match', async () => {
    useTour.mockReturnValue({ tour: null });
    resolveWith(() => Promise.resolve({ leads: [], listings: [] }));
    crmPost.mockResolvedValue({
      lead_id: LEAD_ID,
      created: false,
      address: '100 Main St',
      identity: { status: 'possible_match', basis: 'address_exact', verified: false },
    });
    await lookUp('100 Main St, DE');
    fireEvent.click(screen.getByRole('button', { name: /create a new property record/i }));
    const candidate = await screen.findByRole('button', { name: /^Lead\s*100 Main St/ });
    expect(candidate.textContent).toMatch(/possible match/i);
    expect(subjectWasSelected()).toBe(false);
    expect(screen.queryByText(/matched an existing record/i)).toBeNull();
    expect(screen.getByText(/add the city and ZIP/i)).toBeTruthy();

    // Picking it answers the question; the prompt does not follow it.
    fireEvent.click(candidate);
    await waitFor(() => expect(subjectWasSelected()).toBe(true));
    expect(screen.queryByText(/add the city and ZIP/i)).toBeNull();
  });

  it('selects a reused record whose address matched exactly, and says it is not verified', async () => {
    useTour.mockReturnValue({ tour: null });
    resolveWith(() => Promise.resolve({ leads: [], listings: [] }));
    crmPost.mockResolvedValue({
      lead_id: LEAD_ID, created: false, address: '1 Main St, Dover, DE 19901', identity: EXACT,
    });
    await lookUp('1 Main St, Dover, DE 19901');
    fireEvent.click(screen.getByRole('button', { name: /create a new property record/i }));
    await waitFor(() => expect(subjectWasSelected()).toBe(true));
    expect(screen.getByText(/matched an existing record .*not verified/i)).toBeTruthy();
  });

  it('does not select the same words in a state when the typed address names no town', async () => {
    useTour.mockReturnValue({ tour: null });
    resolveWith(() => Promise.resolve({
      leads: [{
        id: LEAD_ID,
        address: '100 Main St, DE',
        identity: { status: 'possible_match', basis: 'address_exact', verified: false },
      }],
      listings: [],
    }));
    await lookUp('100 Main St, DE');
    expect(subjectWasSelected()).toBe(false);
    expect(screen.getByRole('button', { name: /100 Main St, DE/ }).textContent).toMatch(/possible match/i);
  });
});

describe('the chosen record stays named after the picker closes (T11.03, F11.04)', () => {
  /** The bug: picking a possible match unmounted the picker, the only element
   *  that named the record and its "Possible match" flag, while the page and
   *  the tour viewer went on showing the typed address as the subject. */
  const attachingTo = () => screen.getByRole('group', { name: /record media attaches to/i });

  async function pickPossibleMatch() {
    crmGet.mockImplementation((url) => {
      if (url.startsWith('/api/crm/property-view/resolve')) {
        return Promise.resolve({
          leads: [{ id: LEAD_ID, address: '112 Main St, Dover, DE 19901', identity: POSSIBLE }], listings: [],
        });
      }
      return routeGet(url);
    });
    render(<PropertyViewTab />);
    fireEvent.change(screen.getByLabelText(/property address/i), { target: { value: '12 Main St, Dover, DE 19901' } });
    fireEvent.submit(screen.getByLabelText(/property address/i).closest('form'));
    fireEvent.click(await screen.findByRole('button', { name: /112 Main St/ }));
    await screen.findByRole('region', { name: /3d tour/i });
  }

  it('names a picked possible match and says it is only possible', async () => {
    useTour.mockReturnValue({ tour: null });
    await pickPossibleMatch();
    expect(attachingTo().textContent).toMatch(/112 Main St, Dover, DE 19901/);
    expect(attachingTo().textContent).toMatch(/possible match/i);
  });

  it('titles the walk with the chosen record, not the typed address', async () => {
    useTour.mockReturnValue({
      tour: { splat_url: '/public/splats/a.splat', is_this_property: true, pano_scene_count: 0 },
    });
    await pickPossibleMatch();
    fireEvent.click(screen.getByRole('button', { name: /step inside/i }));
    const viewer = await screen.findByTestId('tour-viewer');
    expect(viewer.getAttribute('data-title')).toBe('112 Main St, Dover, DE 19901');
  });

  it('goes back to the picker to change the record', async () => {
    useTour.mockReturnValue({ tour: null });
    await pickPossibleMatch();
    fireEvent.click(screen.getByRole('button', { name: /change record/i }));
    await screen.findByRole('heading', { name: /attach media to a record/i });
    expect(screen.queryByRole('region', { name: /3d tour/i })).toBeNull();
    expect(screen.getByRole('button', { name: /112 Main St/ }).textContent).toMatch(/possible match/i);
  });

  it('names an auto-selected exact address as not verified', async () => {
    useTour.mockReturnValue({ tour: null });
    await lookUpAProperty();
    expect(attachingTo().textContent).toMatch(/1 Main St, Dover, DE 19901/);
    expect(attachingTo().textContent).toMatch(/address match · not verified/i);
  });

  it('names a record it just created, and keeps it in the picker', async () => {
    useTour.mockReturnValue({ tour: null });
    crmGet.mockImplementation((url) => {
      if (url.startsWith('/api/crm/property-view/resolve')) return Promise.resolve({ leads: [], listings: [] });
      return routeGet(url);
    });
    crmPost.mockResolvedValue({ lead_id: LEAD_ID, created: true });
    render(<PropertyViewTab />);
    fireEvent.change(screen.getByLabelText(/property address/i), { target: { value: '9 Elm St, Dover, DE 19901' } });
    fireEvent.submit(screen.getByLabelText(/property address/i).closest('form'));
    fireEvent.click(await screen.findByRole('button', { name: /create a new property record/i }));
    await screen.findByRole('region', { name: /3d tour/i });
    expect(attachingTo().textContent).toMatch(/9 Elm St, Dover, DE 19901/);
    expect(attachingTo().textContent).toMatch(/new record/i);

    fireEvent.click(screen.getByRole('button', { name: /change record/i }));
    await screen.findByRole('heading', { name: /attach media to a record/i });
    expect(screen.queryByText(/no existing record matches/i)).toBeNull();
    expect(screen.getByRole('button', { name: /9 Elm St/ }).textContent).toMatch(/new record/i);
    expect(screen.queryByText(/may be another property/i)).toBeNull();
  });
});
