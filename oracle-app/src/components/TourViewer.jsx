import { Suspense, lazy, useMemo } from 'react';

/**
 * TourViewer — one entry point, two WebGL engines.
 *
 * `VITE_TOUR_ENGINE` selects which renderer draws a tier-3 walkable tour:
 *   'playcanvas' (default) → PropertyTourViewer: .glb meshes (needed by the
 *                            floor-plan 3D layout boxes), orbit mode and
 *                            multi-floor navigation
 *   'gsplat'               → WalkableSplatViewer, the original raw-WebGL viewer,
 *                            kept as the fallback if a device struggles
 *
 * Both read splats as `.splat` only. PLY is training output, not a delivery
 * format — reconstruction_worker converts it before the URL is ever served.
 *
 * Both are lazy — whichever engine is unused never enters the bundle graph for
 * a session, and neither loads at all until a tour is actually opened.
 *
 * PlayCanvas is the default as of 2026-08-07. gsplat remains one env var away
 * (VITE_TOUR_ENGINE=gsplat) because the mobile-hardware validation that was
 * meant to gate this switch has still not been run — if a device regresses,
 * that is the lever, not a code change.
 * See SYPHER_VAULT/10_Active_Builds/Neoh_Walkable_Tours.md.
 */

const WalkableSplatViewer = lazy(() => import('./WalkableSplatViewer'));
const PropertyTourViewer = lazy(() => import('./PropertyTourViewer'));
// Tier 2. Raw WebGL and no engine dependency at all, so it is a much smaller
// chunk than either splat renderer — which matters because it is the tier most
// properties will actually reach.
const PanoViewer = lazy(() => import('./PanoViewer'));

const ENGINE = (import.meta.env.VITE_TOUR_ENGINE || 'playcanvas').toLowerCase();

export function TourViewer({
  splatUrl, panoScenes, disclosure, address, title, floors, onClose, isThisProperty = true,
}) {
  // PropertyTourViewer re-initialises its whole engine when `assets` changes
  // identity, so this must be stable across re-renders.
  const assets = useMemo(
    () => (splatUrl ? [{ id: 'property-splat', url: splatUrl }] : []),
    [splatUrl],
  );

  // A generated demo space renders identically to a real capture, so the only
  // thing separating "this is your house" from "this is a sample room" is what
  // the viewer says. Prepend it to the disclosure both engines already display,
  // rather than adding a second overlay each engine would have to implement.
  const shownDisclosure = isThisProperty
    ? disclosure
    : [
        'This is a generated demo space, not a capture of this property.',
        disclosure,
      ].filter(Boolean).join(' ');

  // Tier 3 outranks tier 2 when a real capture exists, but a demo splat does
  // not: 360s of the actual property beat a generated room every time.
  const preferPano = Array.isArray(panoScenes) && panoScenes.length >= 2
    && (!splatUrl || !isThisProperty);

  if (preferPano) {
    return (
      <Suspense fallback={null}>
        <PanoViewer
          scenes={panoScenes}
          disclosure={disclosure}
          address={address}
          title={title}
          onClose={onClose}
        />
      </Suspense>
    );
  }

  if (!splatUrl) return null;

  if (ENGINE === 'playcanvas') {
    return (
      <Suspense fallback={null}>
        <PropertyTourViewer
          assets={assets}
          floors={floors || []}
          address={address}
          title={isThisProperty ? title : `${title} — demo space`}
          aiGenerated
          disclosure={shownDisclosure}
          onClose={onClose}
        />
      </Suspense>
    );
  }

  return (
    <Suspense fallback={null}>
      <WalkableSplatViewer
        splatUrl={splatUrl}
        disclosure={shownDisclosure}
        address={address}
        title={isThisProperty ? title : `${title} — demo space`}
        onClose={onClose}
      />
    </Suspense>
  );
}

export default TourViewer;
