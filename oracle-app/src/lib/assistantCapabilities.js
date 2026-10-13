import snapshot from './assistantCapabilities.json';

/**
 * The frontend half of the A09 guard.
 *
 * `assistantCapabilities.json` is generated from backend/ai_capabilities.py
 * (the registry of what Neoh can actually run) and a backend test fails when
 * it is stale. The page itself reads the live API (GET
 * /api/ai/chat/capabilities); this file exists so hand-written chat prompts in
 * the UI can be checked against the same registry in a unit test.
 */
export const CAPABILITY_SNAPSHOT = snapshot;

/**
 * Every affirmative claim in `text` whose area has no executable tool, as
 * `{ pattern, sentence }`. Same rules as the backend's `unbacked_claims`: a
 * sentence that negates ("not", "never", "no"…) is a disclaimer and skipped.
 */
export function unbackedClaims(text, registry = snapshot) {
  const usable = new Set(
    registry.areas.filter((area) => area.state !== 'unavailable').map((area) => area.id),
  );
  const negation = new RegExp(registry.negation, 'i');
  const found = [];
  for (const sentence of String(text || '').split(new RegExp(registry.sentence_split))) {
    if (!sentence || !sentence.trim() || negation.test(sentence)) continue;
    for (const { pattern, area } of registry.claim_terms) {
      if (area !== null && usable.has(area)) continue;
      if (new RegExp(pattern, 'i').test(sentence)) found.push({ pattern, sentence: sentence.trim() });
    }
  }
  return found;
}
