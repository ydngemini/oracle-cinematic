/**
 * searchModel — the pure half of the Work search box.
 *
 * Everything here is a function of its inputs so the box's behaviour can be
 * pinned without rendering: which chips exist, how hits group, what the
 * empty state says and why. The fetch lives in the component.
 */

/** The four searchable kinds, in the order the chips render. Other Work
 *  types (opportunities, sales, …) are views, not search kinds, and are
 *  reached by ?type= or a link — never by typing. */
export const SEARCH_KINDS = Object.freeze([
  { id: 'people', label: 'People' },
  { id: 'properties', label: 'Properties' },
  { id: 'deals', label: 'Deals' },
  { id: 'conversations', label: 'Conversations' },
]);

export const KIND_LABELS = Object.freeze(
  Object.fromEntries(SEARCH_KINDS.map((k) => [k.id, k.label]).concat([['records', 'Public records']])),
);

/** Fewer characters than this and the API returns nothing by design. */
export const MIN_QUERY = 2;

/** Whether a Work type is one the search box searches, or a plain view. */
export function isSearchKind(type) {
  return SEARCH_KINDS.some((k) => k.id === type);
}

/**
 * Group hits by kind, in chip order, dropping empty groups. The API already
 * ranks within and across kinds; this only decides the headings, and it puts
 * the kind the agent has selected first so the chip and the list agree.
 */
export function groupHits(results, selectedKind = null) {
  const order = SEARCH_KINDS.map((k) => k.id).concat(['records']);
  if (selectedKind && order.includes(selectedKind)) {
    order.splice(order.indexOf(selectedKind), 1);
    order.unshift(selectedKind);
  }
  const groups = new Map(order.map((k) => [k, []]));
  for (const hit of results || []) {
    if (!groups.has(hit.kind)) groups.set(hit.kind, []);
    groups.get(hit.kind).push(hit);
  }
  return [...groups.entries()]
    .filter(([, hits]) => hits.length > 0)
    .map(([kind, hits]) => ({ kind, label: KIND_LABELS[kind] || kind, hits }));
}

/**
 * What to say when there is nothing to show. The difference between "no
 * match" and "a leg failed" is the whole reason the API returns `degraded`,
 * and it must survive to the sentence the agent reads.
 */
export function emptyMessage(query, response) {
  const q = (query || '').trim();
  if (q.length < MIN_QUERY) return null;
  const degraded = response?.degraded || [];
  if (degraded.length > 0) {
    const names = degraded.map((k) => (KIND_LABELS[k] || k).toLowerCase()).join(', ');
    return `Nothing found for “${q}” — but ${names} could not be searched just now, so this may be incomplete.`;
  }
  return `Nothing matches “${q}”.`;
}

/** The degraded banner, or null. Shown above results even when there ARE
 *  results, because a partial answer that looks complete is worse than an
 *  empty one. */
export function degradedMessage(response) {
  const degraded = response?.degraded || [];
  if (degraded.length === 0) return null;
  const names = degraded.map((k) => (KIND_LABELS[k] || k).toLowerCase()).join(', ');
  return `${names.charAt(0).toUpperCase()}${names.slice(1)} could not be searched just now. Results below are from the other kinds.`;
}
