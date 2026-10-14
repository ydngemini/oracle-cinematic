/**
 * How the UI words a per-source lookup status (CONTRACT C6: checked,
 * timed_out, unavailable, not_applicable).
 *
 * The backend returns `source_status` next to public-data answers
 * (enrich-property, footprints, geocoding — audit A02, ledger L11-03). A
 * blank or empty value only means "none" when its source was `checked`; after
 * a timeout or an error it means "couldn't check", and the page must say so.
 * Same rule as propertyLookupStatus.js, which covers the client steward.
 */

const FAILED = new Set(['timed_out', 'unavailable']);

export function isFailed(entry) {
  return Boolean(entry && typeof entry === 'object' && FAILED.has(entry.status));
}

const REASON_TEXT = {
  not_configured: 'not configured',
  credential_expired: 'credential expired',
  credential_rejected: 'credential rejected',
  rate_limited: 'rate limited',
  upstream_timeout: 'timed out',
  no_coordinates: 'needs map coordinates',
  no_address: 'needs an address',
  cache_unavailable: 'data cache unavailable',
};

/** Short reason for a source that was not checked, e.g. "timed out". */
export function reasonText(entry) {
  if (!entry || typeof entry !== 'object') return 'not checked';
  if (entry.reason && REASON_TEXT[entry.reason]) return REASON_TEXT[entry.reason];
  if (entry.status === 'timed_out') return 'timed out';
  return 'unavailable';
}

/**
 * The text for one fact cell.
 * @param {object|undefined} entry   the source's status entry
 * @param {boolean} hasValue         whether the source produced a value
 * @param {() => string} render      formats the value
 * @param {string} checkedEmpty      text for a verified "none"
 */
export function factText(entry, hasValue, render, checkedEmpty = 'None on record') {
  if (isFailed(entry)) {
    return entry.reason === 'not_configured' ? 'Not configured' : "Couldn't check";
  }
  if (hasValue) return render();
  if (entry?.status === 'checked') return checkedEmpty;
  return 'Not checked';
}

const FOOTPRINT_SOURCES = {
  regrid: 'licensed parcel data',
  openstreetmap: 'OpenStreetMap',
};

/**
 * What an empty footprint search means, from the backend's `lookup`
 * (found | none_found | incomplete | not_checked) and `source_status`.
 * @returns {{ failed: boolean, text: string } | null} null when candidates exist.
 */
export function footprintLookupView(lookup, sourceStatus) {
  if (lookup === 'found') return null;
  const entries = Object.entries(sourceStatus || {});
  const notChecked = entries
    .filter(([, entry]) => entry?.status !== 'checked')
    .map(([name, entry]) => `${FOOTPRINT_SOURCES[name] || name} ${reasonText(entry)}`);
  const detail = notChecked.length ? ` (${notChecked.join('; ')})` : '';
  if (lookup === 'not_checked') {
    return {
      failed: true,
      text: `Couldn't check for a building outline${detail}. This is not a "no footprint" result.`,
    };
  }
  if (lookup === 'incomplete') {
    return {
      failed: true,
      text: `No outline from the sources that answered, but not every source could be checked${detail}. `
        + 'This is not a "no footprint" result; try again later.',
    };
  }
  // none_found, or a backend from before `lookup` existed.
  return {
    failed: false,
    text: 'No building outline found for this address — rural OSM coverage is '
      + 'patchy and not every address matches a licensed record.',
  };
}
