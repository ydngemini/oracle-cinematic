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

import { cleanup, render, screen, fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

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
  default: ({ title, disclosure }) => (
    <div data-testid="splat" data-title={title} data-disclosure={disclosure} />
  ),
}));
vi.mock('./WalkableSplatViewer', () => ({ default: () => <div data-testid="gsplat" /> }));

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
