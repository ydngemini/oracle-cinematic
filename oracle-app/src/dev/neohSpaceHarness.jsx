/**
 * Neoh Space harness (dev server only — see neoh-space-harness.html).
 *
 * Renders a minimal property page with the real TourViewer on top of it, driven
 * by query parameters, so a browser test can exercise the production viewer
 * code against fixtures:
 *
 *   ?asset=/space-fixture/space.sog&format=.sog&scene=/space-fixture/scene.json
 *   &device=auto|force|none   (assessment from the browser / forced capable / forced incapable)
 *   &embedded=1
 *
 * `window.__neohSpace` exposes open()/close() for memory cycling and a counter
 * of live viewer canvases.
 */
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';

import '../index.css';
import { TourViewer } from '../components/TourViewer';
import { assessDevice, probeDevice } from '../lib/tour/deviceCapability';

const params = new URLSearchParams(window.location.search);
const asset = params.get('asset') || '/space-fixture/space.splat';
const format = params.get('format') || '.splat';
const sceneUrl = params.get('scene');
const deviceMode = params.get('device') || 'auto';
const embedded = params.get('embedded') === '1';

const FORCED = { canRender3D: true, reason: null, quality: params.get('quality') || 'balanced', maxPixelRatio: 1.5, constrainedNetwork: false };
const NONE = { canRender3D: false, reason: '3D view unavailable on this device.', quality: 'performance', maxPixelRatio: 1, constrainedNetwork: false };

// An entry module (mounted by createRoot below), not a component library, so
// fast-refresh's export rule does not apply.
// eslint-disable-next-line react-refresh/only-export-components
function Harness() {
  const [open, setOpen] = useState(true);
  const [scene, setScene] = useState(null);
  const [sceneReady, setSceneReady] = useState(!sceneUrl);
  const [device] = useState(() => {
    if (deviceMode === 'force') return FORCED;
    if (deviceMode === 'none') return NONE;
    const snapshot = probeDevice();
    window.__neohSpaceProbe = snapshot;
    return assessDevice(snapshot);
  });

  useEffect(() => {
    if (!sceneUrl) return;
    fetch(sceneUrl).then((r) => (r.ok ? r.json() : null)).then((s) => {
      setScene(s);
      setSceneReady(true);
    }, () => setSceneReady(true));
  }, []);

  useEffect(() => {
    window.__neohSpace = {
      open: () => setOpen(true),
      close: () => setOpen(false),
      device,
      liveCanvases: () => document.querySelectorAll('canvas').length,
    };
  }, [device]);

  return (
    <main style={{ padding: 16, fontFamily: 'system-ui', color: '#ddd', background: '#111', minHeight: '100vh' }}>
      <h1 style={{ fontSize: 18 }}>12 Harness Lane (fixture property)</h1>
      <section aria-label="Photos"><p data-testid="property-photos">24 photos · floor plan · listing details</p></section>
      <button type="button" onClick={() => setOpen(true)}>Open Neoh Space</button>
      {open && sceneReady ? (
        <TourViewer
          splatUrl={asset}
          splatStreamUrl={asset}
          splatFormat={format}
          splatScene={scene}
          panoScenes={[]}
          title="12 Harness Lane"
          address="12 Harness Lane"
          disclosure="Fixture space for automated testing."
          photoCount={24}
          roomNames={['Living Room', 'Kitchen']}
          embedded={embedded}
          device={device}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </main>
  );
}

createRoot(document.getElementById('root')).render(<StrictMode><Harness /></StrictMode>);
