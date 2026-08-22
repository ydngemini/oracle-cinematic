/**
 * What a surface is allowed to offer for a property's tour, and what it must
 * say when it can offer nothing.
 *
 * This existed twice as inline JSX in HouseWorkspace and nowhere else, so the
 * one surface that starts captures (PropertyViewTab) rendered no tour at all.
 * Two problems came out of that, and this module fixes both:
 *
 *  1. Drift. The "is this actually this house" wording is a claim about
 *     evidence, not a caption. Two hand-maintained copies of it will diverge,
 *     and the copy that diverges is the one that over-promises.
 *
 *  2. Silence reads as absence. When the resolver came back below tier 2 the
 *     button simply did not render — indistinguishable, to a user looking for
 *     the feature, from the feature not existing. `unavailable` carries a
 *     reason precisely so a surface can say "not captured yet" instead of
 *     showing nothing.
 *
 * Mirrors the server's tier rule (backend/tour_api.py): tier 3 is a splat,
 * tier 2 is two or more 360 scenes. One 360 is a view, not a walkthrough, and
 * `isWalkable` in ./panoGraph draws that same line.
 */

/** The subset of `GET /api/crm/property-tour` this decision reads. */
export interface TourResolution {
  splat_url?: string | null;
  pano_scene_count?: number | null;
  /** False when the splat is a stand-in rather than a capture of this address. */
  is_this_property?: boolean | null;
  photo_count?: number | null;
}

export type TourOffer =
  | {
      kind: 'walkable';
      /** Button text. Never claims the space is this home unless it is. */
      label: string;
      /** A walkable space that is NOT a capture of this address. */
      isDemo: boolean;
    }
  | {
      kind: 'unavailable';
      /** Shown to the user. States what is missing, not that nothing exists. */
      reason: string;
    };

/**
 * Decide what to offer. `null`/`undefined` means the resolver did not answer —
 * treated as "not known", never as "none", because a failed fetch is not
 * evidence about the property.
 */
export function tourOffer(tour: TourResolution | null | undefined): TourOffer {
  if (!tour) {
    return { kind: 'unavailable', reason: 'Tour status unavailable — could not reach the resolver.' };
  }

  const panoCount = Number(tour.pano_scene_count) || 0;
  const hasSplat = Boolean(tour.splat_url);
  // The server already applies this rule; re-stating it here keeps the button
  // from appearing on a payload that only *looks* sufficient.
  const walkablePano = panoCount >= 2;

  if (!hasSplat && !walkablePano) {
    if (panoCount === 1) {
      return {
        kind: 'unavailable',
        reason: 'One 360° photo captured. A walkthrough needs at least two so there is a route between them.',
      };
    }
    const photos = Number(tour.photo_count) || 0;
    return {
      kind: 'unavailable',
      reason: photos > 0
        ? `No 3D tour yet — ${photos} photo${photos === 1 ? '' : 's'} on file. Start a capture to build one.`
        : 'No 3D tour yet. Upload photos, then start a capture to build one.',
    };
  }

  // A stand-in splat is walkable but is not this house. Saying "step inside"
  // over a generated room invites the reader to believe they are seeing the
  // property — the one claim the data cannot support.
  const isDemo = tour.is_this_property === false && !panoCount;
  if (isDemo) {
    return { kind: 'walkable', label: 'Preview a demo 3D space (not this home)', isDemo: true };
  }

  return {
    kind: 'walkable',
    label: hasSplat && tour.is_this_property !== false
      ? 'Step inside · walk the 3D space'
      : 'Step inside · walk the 360° tour',
    isDemo: false,
  };
}
