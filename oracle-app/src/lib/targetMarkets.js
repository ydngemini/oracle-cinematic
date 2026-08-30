/**
 * The agent's target markets, from a field whose shape is not guaranteed.
 *
 * `target_markets` is a jsonb column written by more than one path, so it
 * arrives as an array from the profile API and as a comma-separated string
 * from older rows. MyProfileTab has carried a private copy of this normalizer
 * with the note "never trust shape"; it lives here now so the market panel and
 * the profile card cannot disagree about what a market list is.
 */
export function toMarkets(value) {
  if (Array.isArray(value)) return value.map((m) => String(m).trim()).filter(Boolean);
  if (typeof value === 'string') return value.split(',').map((m) => m.trim()).filter(Boolean);
  return [];
}

/**
 * The subset of markets that look like US ZIP codes.
 *
 * Onboarding asks for ZIPs, but the same field also holds free-text city names
 * typed elsewhere. Only a ZIP can be handed to the public-records search, and
 * sending "Wilmington" as `zip=` would return an empty list that reads as
 * "no properties here" rather than "that is not a ZIP".
 */
export function zipMarkets(value) {
  return toMarkets(value).filter((m) => /^\d{5}$/.test(m));
}
