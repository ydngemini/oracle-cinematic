import { Suspense, lazy, useMemo, useState } from 'react';

import useProtectedMedia from '../state/useProtectedMedia';
import styles from './TourViewer.module.css';

/**
 * TourViewer — one tour, every asset the property actually has.
 *
 * This used to pick a single winner and render only that. A property holding a
 * 3D capture AND 360s showed the capture and silently dropped the 360s; a
 * property holding 360s but no capture hit `if (!splatUrl) return null` and
 * opened nothing at all. Both discarded work an agent had already paid to do.
 *
 * So the modes compose. Whatever exists is offered, the switcher moves between
 * them, and the most immersive *real* asset opens first — an ordering, not a
 * filter.
 *
 * Honesty is per asset, which is the other half of the same bug. There was one
 * `isThisProperty` flag for the whole tour, computed from the splat, so a
 * generated demo space beside genuine 360s of the home marked everything "not
 * this property" — and the real 360s were suppressed to avoid the
 * contradiction. Now the badge describes the mode you are looking at, so
 * stepping from a real 360 into a demo capture changes what the viewer claims.
 *
 * `VITE_TOUR_ENGINE` still selects which renderer draws a walkable capture:
 *   'playcanvas' (default) → PropertyTourViewer: .glb meshes (needed by the
 *                            floor-plan 3D layout boxes), orbit mode and
 *                            multi-floor navigation
 *   'gsplat'               → WalkableSplatViewer, the original raw-WebGL viewer,
 *                            kept as the fallback if a device struggles
 *
 * Delivery is `.sog`; `.splat` still loads for assets recorded before that
 * changed. PLY is training output, not a delivery format — reconstruction_worker
 * converts it before the URL is ever served. Note the gsplat fallback cannot
 * read `.sog` (its package ships no SOG loader) and says so rather than
 * failing blank.
 *
 * **Protected media is resolved HERE, once, for every renderer.** The
 * reconstruction worker stores a finished splat behind `/api/media/{id}`, which
 * requires the Neoh JWT. PlayCanvas's internal asset request does not carry our
 * Authorization header, so handing it that URL produced a 401 and a black
 * canvas — the splat had rendered for nobody since the media route was
 * protected. `useProtectedMedia` fetches the bytes with the app's own client
 * and yields a `blob:` URL, and the ORIGINAL filename travels beside it because
 * a blob URL has no extension for PlayCanvas to infer a parser from.
 *
 * Doing it at this level rather than inside each viewer is deliberate: two
 * renderers independently deciding how to authenticate is how one of them ends
 * up not doing it.
 *
 * Every renderer is lazy — an engine that is never opened stays out of the
 * bundle graph for the session.
 *
 * PlayCanvas is the default as of 2026-08-07. gsplat remains one env var away
 * because the mobile-hardware validation meant to gate the switch has still not
 * been run — if a device regresses, that is the lever, not a code change.
 * See SYPHER_VAULT/10_Active_Builds/Neoh_Walkable_Tours.md.
 */

const WalkableSplatViewer = lazy(() => import('./WalkableSplatViewer'));
const PropertyTourViewer = lazy(() => import('./PropertyTourViewer'));
// Raw WebGL and no engine dependency at all, so it is a much smaller chunk than
// either splat renderer — which matters because it is the mode most properties
// will actually have.
const PanoViewer = lazy(() => import('./PanoViewer'));

const ENGINE = (import.meta.env.VITE_TOUR_ENGINE || 'playcanvas').toLowerCase();

const DEMO_PREFIX = 'This is a generated demo space, not a capture of this property.';

export function TourViewer({
  splatUrl, panoScenes, disclosure, address, title, floors, onClose,
  isThisProperty = true, tourpoints,
}) {
  // One item in, one out. `useProtectedMedia` fetches /api/media/* with the
  // JWT and returns a blob: URL; anything external passes through untouched.
  // It revokes every URL it made on replacement and unmount.
  const protectedItems = useMemo(
    () => (splatUrl ? [{ id: 'property-splat', url: splatUrl }] : []),
    [splatUrl],
  );
  const [resolvedSplat] = useProtectedMedia(protectedItems);
  const splatBytesUrl = resolvedSplat?.display_url || '';

  // True while the bytes are still being fetched. Mounting the engine now would
  // show a black canvas with no explanation, so the viewer says what it is
  // doing instead.
  const splatPreparing = Boolean(splatUrl) && !splatBytesUrl;

  // PropertyTourViewer re-initialises its whole engine when `assets` changes
  // identity, so this must be stable across re-renders — and must not become a
  // non-empty array until the bytes exist.
  const assets = useMemo(
    () => (splatBytesUrl
      ? [{
        id: 'property-splat',
        url: splatBytesUrl,
        // The format hint. `splatUrl` is the original `/api/media/{id}` or CDN
        // path; the loader reads its extension because `splatBytesUrl` may be
        // a blob: URL that has none.
        filename: splatUrl,
      }]
      : []),
    [splatBytesUrl, splatUrl],
  );

  // Memoised so `modes` can depend on the scenes themselves rather than just
  // their count — it reads each scene's provenance, and a fresh array every
  // render would rebuild the mode list (and reset the switcher) continuously.
  const scenes = useMemo(
    () => (Array.isArray(panoScenes) ? panoScenes : []),
    [panoScenes],
  );

  // Every mode the property supports, each carrying its own standing. A single
  // 360 is included: it is a real view of the home, and hiding it because it is
  // not a full walkthrough is exactly the discard this replaces. It is only
  // labelled honestly — "360° view", not "walkthrough".
  const modes = useMemo(() => {
    const out = [];
    if (scenes.length) {
      // Read from the scenes rather than assumed. The resolver returns each
      // scene's provenance, and a set that is not wholly captured must not be
      // described as the home — the same rule the capture is held to.
      const allReal = scenes.every((sc) => sc.is_this_property !== false);
      out.push({
        id: 'pano',
        label: scenes.length >= 2 ? '360° walkthrough' : '360° view',
        isThisProperty: allReal,
      });
    }
    if (splatUrl) {
      out.push({
        id: 'splat',
        label: isThisProperty ? 'Full 3D' : 'Demo space',
        isThisProperty,
      });
    }
    return out;
  }, [scenes, splatUrl, isThisProperty]);

  // Which mode opens first. A real capture is the most immersive thing a
  // property can have, but a *generated* one is not evidence about the home, so
  // genuine 360s open ahead of it. This only orders the list — nothing is
  // removed, and the switcher reaches everything.
  const preferredId = useMemo(() => {
    const realPano = modes.find((m) => m.id === 'pano' && m.isThisProperty);
    if (splatUrl && isThisProperty) return 'splat';
    if (realPano && scenes.length >= 2) return 'pano';
    return modes[0]?.id ?? null;
  }, [splatUrl, isThisProperty, scenes, modes]);

  // The guided route, over the same scenes free roam uses. A route is a VIEW of
  // the graph — it holds scene ids, never copies of the scenes — so the two
  // cannot drift apart. Empty means this property has nothing to guide through,
  // which is a normal answer and simply leaves free roam as the only mode.
  const route = useMemo(
    () => (Array.isArray(tourpoints) ? tourpoints : []).filter(
      (point) => scenes.some((sc) => sc.scene_id === point.scene_id),
    ),
    [tourpoints, scenes],
  );
  const [stop, setStop] = useState(null);

  const [chosenId, setChosenId] = useState(null);
  // Resolved during render rather than synced from an effect: if the chosen
  // mode disappears (media deleted while open) it falls back rather than
  // rendering a mode that no longer exists.
  const activeId = modes.some((m) => m.id === chosenId) ? chosenId : preferredId;
  const active = modes.find((m) => m.id === activeId) ?? null;

  if (!active) return null;

  // A generated demo space renders identically to a real capture, so the only
  // thing separating "this is your house" from "this is a sample room" is what
  // the viewer says. Scoped to the active mode, so switching to real 360s stops
  // making the claim.
  const shownDisclosure = active.isThisProperty
    ? disclosure
    : [DEMO_PREFIX, disclosure].filter(Boolean).join(' ');
  const shownTitle = active.isThisProperty ? title : `${title} — demo space`;

  const switcher = modes.length > 1 ? (
    <nav className={styles.switcher} aria-label="Tour views">
      {modes.map((mode) => (
        <button
          key={mode.id}
          type="button"
          className={[
            styles.tab,
            mode.id === activeId ? styles.tabActive : '',
            mode.isThisProperty ? '' : styles.tabDemo,
          ].filter(Boolean).join(' ')}
          aria-current={mode.id === activeId ? 'true' : undefined}
          onClick={() => setChosenId(mode.id)}
        >
          {mode.label}
        </button>
      ))}
    </nav>
  ) : null;

  // Only offered on the 360 route, because that is the mode whose vantage
  // points the route is made of. The splat is one continuous space; there is
  // nothing to step between.
  const guided = activeId === 'pano' && route.length >= 2 ? (
    <nav className={styles.route} aria-label="Guided tour">
      <button
        type="button"
        className={styles.routeStep}
        onClick={() => setStop((at) => Math.max(0, (at ?? 0) - 1))}
        disabled={(stop ?? 0) <= 0}
        aria-label="Previous stop"
      >
        ‹
      </button>
      <span className={styles.routeLabel}>
        {stop === null
          ? `Guided tour · ${route.length} stops`
          : `${route[stop].label} · ${stop + 1} of ${route.length}`}
      </span>
      <button
        type="button"
        className={styles.routeStep}
        onClick={() => setStop((at) => Math.min(route.length - 1, (at ?? -1) + 1))}
        disabled={stop !== null && stop >= route.length - 1}
        aria-label={stop === null ? 'Start the guided tour' : 'Next stop'}
      >
        ›
      </button>
    </nav>
  ) : null;

  let viewer;
  if (activeId === 'pano') {
    viewer = (
      <PanoViewer
        scenes={scenes}
        disclosure={shownDisclosure}
        address={address}
        title={shownTitle}
        onClose={onClose}
        focusSceneId={stop === null ? null : route[stop]?.scene_id ?? null}
      />
    );
  } else if (splatPreparing) {
    // Deliberate state, not a spinner over an empty engine: the bytes are
    // being fetched with the JWT and there is nothing to draw yet.
    viewer = (
      <div className={styles.preparing} role="status">
        <p>Preparing 3D tour…</p>
      </div>
    );
  } else if (ENGINE === 'playcanvas') {
    viewer = (
      <PropertyTourViewer
        assets={assets}
        floors={floors || []}
        address={address}
        title={shownTitle}
        aiGenerated
        disclosure={shownDisclosure}
        onClose={onClose}
      />
    );
  } else {
    viewer = (
      <WalkableSplatViewer
        splatUrl={splatBytesUrl}
        disclosure={shownDisclosure}
        address={address}
        title={shownTitle}
        onClose={onClose}
      />
    );
  }

  return (
    <>
      <Suspense fallback={null}>{viewer}</Suspense>
      {guided}
      {switcher}
    </>
  );
}

export default TourViewer;
