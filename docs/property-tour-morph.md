# Property to tour continuity

`/property/:id?tour=3d` opens the property's tour inside its existing entity
frame. The frame grows to the viewport width using Neoh's shared spring;
reduced-motion and low-motion-budget devices switch without layout animation.
The address stays in the frame and its close control becomes **Back to property**.

Opening from the frame or embedded dossier pushes one browser-history entry.
Back, Escape, and the back button return to the same mounted dossier, retaining
unsaved input and scroll position. Forward reopens the tour. A direct tour link
uses replacement navigation when returning to the property, so it does not send
the visitor to an unrelated previous page.

The tour renderer stays lazy and uses the existing protected-media loader.
Its embedded mode contains the canvas and tour controls below the frame header;
standalone callers keep their full-window body portal. Captures, 360 scenes,
guided routes, and demo disclosures retain the existing renderer behavior.
Loading, resolver failures, missing captures, and renderer failures leave the
frame's back control available. The inactive dossier is inert while touring.

## Verification

From `oracle-app/`, run `npm test`, `npm run typecheck`, `npm run build`, and
`npm run bundle:check`. Start the built app with `npm run preview -- --port 4175`.
From the repository root, run:

```sh
python3 scripts/test-property-morph-playwright.py --base-url http://localhost:4175
```

The browser check uses synthetic API responses and a small valid GLB mesh, with
the served application and real WebGL renderer. It does not mutate backend
records or validate access to production property captures. It checks desktop
and mobile geometry, actual intermediate animation widths, reduced motion,
low motion budget, history, focus, unsaved input, retained scroll, 360 switching,
and unavailable-tour recovery. Screenshots and results go to
`/tmp/neoh-property-morph/`.

The total-JS allowance increases by 4,549 raw bytes and 1,688 gzip bytes for the
new navigation, focus handling, embedded presentation, and motion. This is the
measured difference from `d050a7e`, built with the same installed dependencies
and environment. The largest-chunk limits and tolerance remain unchanged.
