// @vitest-environment jsdom
import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EntitySheet } from './EntitySheet';
import { crmGet } from '../state/useCrmApi';

vi.mock('../state/useCrmApi', () => ({ crmGet: vi.fn() }));
vi.mock('../components/AssistantContext', () => ({ useAssistantRecord: vi.fn() }));
vi.mock('./motion', () => ({ useMotionPolicy: () => ({ layout: false, transition: { duration: 0 } }) }));
vi.mock('../components/DossierPanel', () => ({
  DossierPanel: ({ onOpenTour }) => (
    <div>
      <input aria-label="Dossier note" defaultValue="Keep this note" />
      <button onClick={onOpenTour}>Dossier tour</button>
    </div>
  ),
}));
vi.mock('../components/TourViewer', () => ({
  default: ({ embedded, splatUrl, splatFormat, splatScene, panoScenes, tourpoints, isThisProperty }) => (
    <div data-testid="tour" data-embedded={embedded} data-url={splatUrl} data-format={splatFormat}
      data-scene={splatScene?.version} data-panos={panoScenes?.length} data-stops={tourpoints?.length}
      data-real={isThisProperty}>
      <button>Tour control</button>
    </div>
  ),
}));

const capture = {
  splat_url: '/api/media/capture', splat_format: '.sog', splat_scene: { version: 1 },
  pano_scenes: [{ scene_id: 'kitchen' }], tourpoints: [{ scene_id: 'kitchen' }],
  is_this_property: true,
};

function Harness({ initiallyOpen = false, onDismiss = () => {} }) {
  const [tour, setTour] = useState(initiallyOpen);
  return <EntitySheet entity={{ kind: 'property', id: 'home', tour }}
    onOpenTour={() => setTour(true)} onClose={() => tour ? setTour(false) : onDismiss()} />;
}

beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', (callback) => setTimeout(callback, 0));
  vi.stubGlobal('cancelAnimationFrame', clearTimeout);
  crmGet.mockImplementation((path) => Promise.resolve(path.includes('/dossier')
    ? { payload: { address: '12 Oak St', city: 'Dover' }, state: 'DE' }
    : capture));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('property to tour continuity', () => {
  // 800ms in isolation, but this is the one test in the file that pays the
  // one-time cost of resolving the lazy DossierPanel + TourViewer imports, and
  // under a saturated CI machine that resolution starves past the default 5s.
  // The seven siblings below reuse the resolved modules and finish in ~60ms;
  // the browser check in scripts/test-property-morph-playwright.py covers the
  // same flow end to end.
  it('opens the real viewer contract and returns to the same editable dossier and focus', async () => {
    const onDismiss = vi.fn();
    render(<Harness onDismiss={onDismiss} />);
    const opener = await screen.findByRole('button', { name: 'Explore in 3D' });
    const note = await screen.findByRole('textbox', { name: 'Dossier note' });
    fireEvent.change(note, { target: { value: 'Unsaved work' } });
    opener.focus();
    fireEvent.click(opener);

    const viewer = await screen.findByTestId('tour');
    expect(viewer.dataset).toMatchObject({ embedded: 'true', url: '/api/media/capture', format: '.sog', scene: '1', panos: '1', stops: '1', real: 'true' });
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(note.closest('[inert]')).toBeTruthy();
    expect(document.activeElement.textContent).toContain('Back to property');
    expect(screen.getAllByRole('dialog')).toHaveLength(1);

    fireEvent.keyDown(document.activeElement, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('tour')).toBeNull());
    expect(screen.getByRole('textbox')).toBe(note);
    expect(note.value).toBe('Unsaved work');
    expect(document.activeElement).toBe(opener);
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.keyDown(document.activeElement, { key: 'Escape' });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  }, 15000);

  it('opens the same experience from the embedded dossier action', async () => {
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Dossier tour' }));
    expect(await screen.findByTestId('tour')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Back to property' }));
    expect(screen.queryByTestId('tour')).toBeNull();
  });

  it('has an exit while a direct tour link is still resolving', async () => {
    crmGet.mockImplementation(() => new Promise(() => {}));
    render(<Harness initiallyOpen />);
    expect(screen.getByRole('status').textContent).toContain('Loading tour');
    fireEvent.click(screen.getByRole('button', { name: 'Back to property' }));
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('distinguishes a failed resolver from a missing capture and keeps the way back', async () => {
    crmGet.mockRejectedValue(new Error('Offline'));
    render(<Harness initiallyOpen />);
    expect((await screen.findByRole('alert')).textContent).toContain('could not be loaded');
    fireEvent.click(screen.getByRole('button', { name: 'Back to property' }));
    expect(screen.getByRole('button', { name: 'Tour status unavailable' }).disabled).toBe(true);
  });

  it('does not offer an absent capture', async () => {
    crmGet.mockResolvedValue({ pano_scenes: [] });
    render(<Harness />);
    expect((await screen.findByRole('button', { name: '3D tour not captured yet' })).disabled).toBe(true);
    expect(screen.queryByTestId('tour')).toBeNull();
  });

  it('keeps demo provenance in both the offer and the viewer', async () => {
    crmGet.mockResolvedValue({ ...capture, is_this_property: false });
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Preview demo 3D space' }));
    expect((await screen.findByTestId('tour')).dataset.real).toBe('false');
  });

  it('can enter on 360 scenes without a splat', async () => {
    crmGet.mockResolvedValue({ pano_scenes: [{ scene_id: 'front' }, { scene_id: 'back' }] });
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Explore 360° tour' }));
    expect((await screen.findByTestId('tour')).dataset.panos).toBe('2');
  });

  it('does not carry a tour or pending record across property identities', async () => {
    const { rerender } = render(<EntitySheet entity={{ kind: 'property', id: 'first', tour: true }} />);
    expect(await screen.findByTestId('tour')).toBeTruthy();
    crmGet.mockImplementation(() => new Promise(() => {}));
    rerender(<EntitySheet entity={{ kind: 'property', id: 'second', tour: true }} />);
    expect(screen.queryByTestId('tour')).toBeNull();
    expect(screen.getByRole('status').textContent).toContain('Loading tour');
  });
});
