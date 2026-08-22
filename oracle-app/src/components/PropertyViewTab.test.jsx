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

const { default: PropertyViewTab } = await import('./PropertyViewTab');

const LEAD_ID = '33333333-3333-4333-8333-333333333333';

/**
 * Drive the real lookup flow so the test exercises the wiring rather than a
 * mock of it: geocode → enrich + resolve → single match auto-selects → view.
 */
function routeGet(url) {
  if (url.startsWith('/api/geocode')) {
    return Promise.resolve({ lat: 39.1, lng: -75.5, display_name: '1 Main St, Dover, DE 19901' });
  }
  if (url.startsWith('/api/enrich-property')) return Promise.resolve(null);
  if (url.startsWith('/api/crm/property-view/resolve')) {
    return Promise.resolve({ leads: [{ id: LEAD_ID, address: '1 Main St' }], listings: [] });
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
