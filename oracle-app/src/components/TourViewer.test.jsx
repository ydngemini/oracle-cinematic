// @vitest-environment jsdom
/**
 * A tour offers every asset the property has.
 *
 * The failure this covers: TourViewer picked exactly one renderer and threw the
 * rest away. A property holding a 3D capture AND 360s rendered the capture and
 * the 360s were unreachable. A property holding 360s but no capture hit
 * `if (!splatUrl) return null` and opened nothing at all — which, to an agent
 * looking for "the tour", is indistinguishable from the feature not existing.
 *
 * The second half is honesty. One `isThisProperty` flag covered the whole tour
 * and was computed from the splat, so a generated demo space sitting beside
 * genuine 360s of the home marked everything "not this property" — and the real
 * 360s were suppressed to avoid the contradiction. The badge now describes the
 * mode on screen, so switching modes changes what the viewer claims.
 */

import { readFileSync } from 'node:fs';

import { cleanup, render, screen, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// vitest.config.js does not set `globals: true`, so Testing Library never
// registers its automatic cleanup — without this, renders accumulate.
afterEach(cleanup);

// Stub the three renderers: this is about which modes are reachable and what
// they are told, not about WebGL. Each reports the props that carry the claim.
vi.mock('./PanoViewer', () => ({
  default: ({ scenes, title, disclosure }) => (
    <div data-testid="pano" data-scenes={scenes.length} data-title={title}
         data-disclosure={disclosure} />
  ),
}));
vi.mock('./PropertyTourViewer', () => ({
  // `assets` is reported because the regression below is entirely about what
  // this component is handed: a protected URL PlayCanvas cannot authenticate.
  default: ({ title, disclosure, assets }) => (
    <div
      data-testid="splat" data-title={title} data-disclosure={disclosure}
      data-asset-url={assets?.[0]?.url ?? ''}
      data-asset-filename={assets?.[0]?.filename ?? ''}
      data-asset-count={assets?.length ?? 0}
    />
  ),
}));
vi.mock('./WalkableSplatViewer', () => ({
  default: ({ splatUrl }) => <div data-testid="gsplat" data-splat-url={splatUrl ?? ''} />,
}));

// useProtectedMedia fetches through this. Resolves immediately by default so
// the ordinary tests see the viewer mount; `holdBlob` keeps it pending for the
// one test that is specifically about the waiting state.
const blobCalls = [];
let holdBlob = false;
let releaseBlob = () => {};
vi.mock('../state/useCrmApi', async (importOriginal) => ({
  ...(await importOriginal()),
  crmGetBlob: (path) => {
    blobCalls.push(path);
    if (!holdBlob) return Promise.resolve(new Blob(['bytes']));
    return new Promise((resolve) => {
      releaseBlob = () => resolve(new Blob(['bytes']));
    });
  },
}));

// jsdom implements neither, and useProtectedMedia depends on both.
beforeEach(() => {
  blobCalls.length = 0;
  holdBlob = false;
  let n = 0;
  URL.createObjectURL = vi.fn(() => `blob:https://neoh.test/${(n += 1)}`);
  URL.revokeObjectURL = vi.fn();
});

const { TourViewer } = await import('./TourViewer');

const SCENES = [{ scene_id: 'a' }, { scene_id: 'b' }];

const renderTour = (props) =>
  render(<TourViewer title="12 Oak St" address="12 Oak St" onClose={() => {}} {...props} />);

describe('a tour with more than one asset', () => {
  it('reaches the 360s even when a real capture exists', async () => {
    renderTour({ splatUrl: '/api/media/x.sog', panoScenes: SCENES, isThisProperty: true });

    // The capture opens first — it is the most immersive real asset.
    expect(await screen.findByTestId('splat')).toBeTruthy();
    // ...but the 360s are still offered, which is what used to be lost.
    const tab = screen.getByRole('button', { name: /360° walkthrough/i });
    fireEvent.click(tab);
    expect(await screen.findByTestId('pano')).toBeTruthy();
  });

  it('offers a single 360 without calling it a walkthrough', async () => {
    renderTour({ splatUrl: '/api/media/x.sog', panoScenes: [{ scene_id: 'a' }] });

    expect(screen.getByRole('button', { name: '360° view' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /360° walkthrough/i })).toBeNull();
  });

  it('shows no switcher when there is only one asset', () => {
    renderTour({ splatUrl: '/api/media/x.sog' });
    expect(screen.queryByRole('navigation', { name: /tour views/i })).toBeNull();
  });
});

describe('a tour with no capture', () => {
  it('still opens on 360s alone', async () => {
    // Previously: `if (!splatUrl) return null` — nothing rendered at all.
    renderTour({ panoScenes: SCENES });
    expect(await screen.findByTestId('pano')).toBeTruthy();
  });

  it('renders nothing only when there is genuinely nothing', () => {
    const { container } = renderTour({ panoScenes: [] });
    expect(container.textContent).toBe('');
  });
});

describe('the claim follows the asset on screen', () => {
  it('opens real 360s ahead of a generated capture', async () => {
    renderTour({ splatUrl: '/api/media/demo.sog', panoScenes: SCENES, isThisProperty: false });

    // A generated room is not evidence about this home, so genuine 360s of it
    // open first. The demo is still reachable, not hidden.
    expect(await screen.findByTestId('pano')).toBeTruthy();
    expect(screen.getByRole('button', { name: /demo space/i })).toBeTruthy();
  });

  it('marks the demo mode on the control itself', () => {
    renderTour({ splatUrl: '/api/media/demo.sog', panoScenes: SCENES, isThisProperty: false });

    const demoTab = screen.getByRole('button', { name: /demo space/i });
    const realTab = screen.getByRole('button', { name: /360° walkthrough/i });
    // The distinction has to survive switching modes, so it lives on the tab
    // rather than only in the overlay text.
    expect(demoTab.className).not.toBe(realTab.className);
  });

  it('only disclaims the demo while the demo is the mode being shown', async () => {
    renderTour({
      splatUrl: '/api/media/demo.sog',
      panoScenes: SCENES,
      isThisProperty: false,
      disclosure: 'AI-generated reconstruction.',
    });

    // On the real 360s, nothing claims this is a demo.
    expect(screen.queryByTestId('splat')).toBeNull();
    expect(screen.getByTestId('pano').dataset.title).toBe('12 Oak St');

    fireEvent.click(screen.getByRole('button', { name: /demo space/i }));

    const splat = await screen.findByTestId('splat');
    expect(splat.dataset.title).toMatch(/demo space/i);
    expect(splat.dataset.disclosure).toMatch(/not a capture of this property/i);
  });

  it('disclaims generated 360s even when the capture is real', async () => {
    // The case that separates per-asset labelling from a single tour-wide flag:
    // the capture genuinely depicts the home, the 360s do not. A tour-wide
    // `isThisProperty` is true here and would let the generated 360s pass
    // themselves off as the property.
    renderTour({
      splatUrl: '/api/media/real.sog',
      isThisProperty: true,
      panoScenes: [
        { scene_id: 'a', is_this_property: false },
        { scene_id: 'b', is_this_property: false },
      ],
      disclosure: 'AI-generated reconstruction.',
    });

    fireEvent.click(screen.getByRole('button', { name: /360° walkthrough/i }));

    const pano = await screen.findByTestId('pano');
    expect(pano.dataset.title).toMatch(/demo space/i);
  });

  it('does not disclaim a genuine capture', async () => {
    renderTour({
      splatUrl: '/api/media/real.sog',
      isThisProperty: true,
      disclosure: 'AI-generated reconstruction.',
    });

    const splat = await screen.findByTestId('splat');
    expect(splat.dataset.title).toBe('12 Oak St');
    expect(splat.dataset.disclosure).toBe('AI-generated reconstruction.');
  });
});

describe('guided route', () => {
  const scenes = [
    { scene_id: 'a', url: '/a.jpg', is_this_property: true, neighbours: [] },
    { scene_id: 'b', url: '/b.jpg', is_this_property: true, neighbours: [] },
    { scene_id: 'c', url: '/c.jpg', is_this_property: true, neighbours: [] },
  ];
  const route = [
    { id: 'tp_a', index: 0, scene_id: 'a', label: 'Kitchen', narration: '' },
    { id: 'tp_b', index: 1, scene_id: 'b', label: 'Living Room', narration: '' },
    { id: 'tp_c', index: 2, scene_id: 'c', label: 'Bedroom', narration: '' },
  ];

  it('offers the route when there is more than one stop', () => {
    render(<TourViewer panoScenes={scenes} tourpoints={route} title="1 Test St" />);
    expect(screen.getByRole('navigation', { name: /guided tour/i })).toBeTruthy();
    expect(screen.getByText(/3 stops/)).toBeTruthy();
  });

  it('is not offered when the property has nothing to guide through', () => {
    render(<TourViewer panoScenes={scenes} tourpoints={[]} title="1 Test St" />);
    expect(screen.queryByRole('navigation', { name: /guided tour/i })).toBeNull();
  });

  it('names the stop it has walked you to', () => {
    render(<TourViewer panoScenes={scenes} tourpoints={route} title="1 Test St" />);

    fireEvent.click(screen.getByRole('button', { name: /start the guided tour/i }));

    expect(screen.getByText(/Kitchen/)).toBeTruthy();
    expect(screen.getByText(/1 of 3/)).toBeTruthy();
  });

  it('drops a stop whose scene no longer exists', () => {
    // Media can be deleted while a tour is open. A route still pointing at it
    // would walk someone into a room that is not there.
    render(<TourViewer panoScenes={[scenes[0]]} tourpoints={route} title="1 Test St" />);
    expect(screen.queryByRole('navigation', { name: /guided tour/i })).toBeNull();
  });
});

describe('a protected splat never reaches PlayCanvas as a URL it cannot authenticate', () => {
  /**
   * The regression. The reconstruction worker stores a finished splat behind
   * `/api/media/{id}`, which requires the Neoh JWT. TourViewer handed that URL
   * straight to PlayCanvas, whose internal asset request carries no
   * Authorization header — so every real reconstruction 401'd and the viewer
   * showed a black canvas. The bytes are now fetched with the app's own
   * authenticated client and passed as a blob: URL.
   */
  const PROTECTED = '/api/media/8d1e2f3a-1111-2222-3333-444455556666.sog';

  it('fetches the media itself instead of passing the protected URL through', async () => {
    renderTour({ splatUrl: PROTECTED });
    const viewer = await screen.findByTestId('splat');

    expect(blobCalls).toContain(PROTECTED);
    const handed = viewer.getAttribute('data-asset-url');
    expect(handed).not.toBe(PROTECTED);
    expect(handed).not.toMatch(/^\/api\/media\//);
    expect(handed).toMatch(/^blob:/);
  });

  it('sends the original filename alongside, because a blob URL has no extension', async () => {
    // Without this PlayCanvas infers the parser from `blob:...`, finds no
    // extension, and loads a Gaussian splat as a generic model.
    renderTour({ splatUrl: PROTECTED });
    const viewer = await screen.findByTestId('splat');
    expect(viewer.getAttribute('data-asset-filename')).toBe(PROTECTED);
  });

  it('waits, visibly, rather than mounting the engine on an empty URL', async () => {
    holdBlob = true;
    renderTour({ splatUrl: PROTECTED });

    expect(screen.queryByTestId('splat')).toBeNull();
    expect(screen.getByText(/Preparing 3D tour/i)).toBeTruthy();

    releaseBlob();
    const viewer = await screen.findByTestId('splat');
    expect(viewer.getAttribute('data-asset-count')).toBe('1');
    expect(screen.queryByText(/Preparing 3D tour/i)).toBeNull();
  });

  it('revokes the object URL when the viewer unmounts', async () => {
    const { unmount } = renderTour({ splatUrl: PROTECTED });
    await screen.findByTestId('splat');
    expect(URL.createObjectURL).toHaveBeenCalled();

    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it('leaves an external CDN splat alone — nothing to authenticate', async () => {
    const cdn = 'https://cdn.example/recon/job-7/model.sog';
    renderTour({ splatUrl: cdn });
    const viewer = await screen.findByTestId('splat');

    expect(blobCalls).toHaveLength(0);
    expect(viewer.getAttribute('data-asset-url')).toBe(cdn);
  });

  it('carries a legacy .splat through the same path', async () => {
    const legacy = '/api/media/0000aaaa-1111-2222-3333-444455556666.splat';
    renderTour({ splatUrl: legacy });
    const viewer = await screen.findByTestId('splat');

    expect(viewer.getAttribute('data-asset-url')).toMatch(/^blob:/);
    // The hint keeps the extension the loader maps to gsplat.
    expect(viewer.getAttribute('data-asset-filename')).toBe(legacy);
  });

  it('gives the gsplat fallback the resolved bytes too, not the protected URL', () => {
    // The fallback is chosen by VITE_TOUR_ENGINE, read once at module load, so
    // it cannot be switched per test. The claim that matters is structural and
    // is checked structurally: ONE resolution above both renderers. Two
    // viewers independently deciding how to authenticate is how one of them
    // ends up not doing it.
    const source = readFileSync('src/components/TourViewer.jsx', 'utf8');
    expect(source).toMatch(/<WalkableSplatViewer[\s\S]*?splatUrl=\{splatBytesUrl\}/);
    expect(source).not.toMatch(/<WalkableSplatViewer[\s\S]*?splatUrl=\{splatUrl\}/);
    // And exactly one call site resolves protected media.
    expect(source.match(/useProtectedMedia\(/g)).toHaveLength(1);
  });

  it('still switches between 360 and 3D when the property has both', async () => {
    renderTour({ splatUrl: PROTECTED, panoScenes: SCENES });
    await screen.findByTestId('splat');

    fireEvent.click(screen.getByRole('button', { name: /360/i }));
    expect(await screen.findByTestId('pano')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /Full 3D/i }));
    const back = await screen.findByTestId('splat');
    expect(back.getAttribute('data-asset-url')).toMatch(/^blob:/);
  });
});
