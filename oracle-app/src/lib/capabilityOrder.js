// Kept apart from assistantCapabilities.js so the page does not bundle the
// test-only registry snapshot.

// Usable areas first, in the order a person would act on them.
const STATE_ORDER = ['available', 'degraded', 'approval_gated', 'setup_required', 'unavailable'];

export function groupAreas(areas) {
  const rank = (state) => {
    const index = STATE_ORDER.indexOf(state);
    return index === -1 ? STATE_ORDER.length : index;
  };
  return [...(Array.isArray(areas) ? areas : [])].sort((a, b) => rank(a.state) - rank(b.state));
}
