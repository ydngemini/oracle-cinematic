/**
 * BorderBeam — RETIRED (Mission 3, HUD removal).
 *
 * This used to orbit a gold light around a panel's border: sci-fi chrome that
 * signalled nothing the surrounding UI did not already say. It now renders
 * nothing, so the two remaining call sites (CrmShell's profile sheet and
 * PersonalCommandComposer's busy state — both owned elsewhere) go calm without
 * an edit. Busy state is announced by AgentStatusBar's role="status" line.
 *
 * Delete this file once those imports are gone.
 */
export function BorderBeam() {
  return null;
}
