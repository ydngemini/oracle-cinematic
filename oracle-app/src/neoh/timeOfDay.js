/**
 * timeOfDay — how Home arranges itself around the hour.
 *
 * The screen never changes WHAT is shown, only the order and what is
 * collapsed. That distinction is the whole design: a home that hides an
 * opportunity because it is 9pm has made a decision on the agent's behalf; a
 * home that leads with what is due in the next 90 minutes has made a good
 * guess and left everything else one scroll away.
 *
 * Pure. Takes the briefing and a Date, returns an arrangement. No fetches,
 * no LLM, so it costs nothing and can be tested exhaustively.
 */

/** Minutes ahead within which a dated item counts as "about to happen". */
export const IMMINENT_MINUTES = 90;

/** How many items Home shows. Three is the ceiling the vision set: "3 things
 *  need you" is a sentence, seven is a list. */
export const HOME_ITEM_LIMIT = 3;

export function modeFor(now, horizon = []) {
  const imminent = imminentItem(now, horizon);
  if (imminent) return 'pre_appointment';
  const hour = now.getHours();
  if (hour < 11) return 'morning';
  if (hour >= 17) return 'evening';
  return 'default';
}

/** The nearest dated opportunity that starts within IMMINENT_MINUTES. */
export function imminentItem(now, horizon = []) {
  const soon = now.getTime() + IMMINENT_MINUTES * 60_000;
  let best = null;
  for (const bucket of horizon) {
    if (bucket?.key !== 'now' && bucket?.key !== 'today') continue;
    for (const item of bucket.items || []) {
      const at = Date.parse(item.deadline || '');
      if (!Number.isFinite(at) || at < now.getTime() || at > soon) continue;
      if (!best || at < best.at) best = { at, item };
    }
  }
  return best ? best.item : null;
}

function byDeadlineThenScore(a, b) {
  const ad = Date.parse(a.deadline || '');
  const bd = Date.parse(b.deadline || '');
  const aHas = Number.isFinite(ad);
  const bHas = Number.isFinite(bd);
  if (aHas && bHas && ad !== bd) return ad - bd;
  if (aHas !== bHas) return aHas ? -1 : 1;
  return (b.score ?? 0) - (a.score ?? 0);
}

function byScore(a, b) {
  return (b.score ?? 0) - (a.score ?? 0);
}

/**
 * Arrange the briefing for the hour.
 *
 * Returns the items to show (at most HOME_ITEM_LIMIT, in order), how many
 * more exist beyond them, and two presentation flags: whether the "handled"
 * list opens expanded, and whether the decision controls are shown at all —
 * in the evening the screen is a record of the day, not a to-do list, and
 * putting "I'll do this" under every card at 9pm is a nag.
 */
export function arrange(briefing, now = new Date()) {
  const opportunities = briefing?.attention?.opportunities ?? [];
  const horizon = briefing?.horizon ?? [];
  const mode = modeFor(now, horizon);

  let ordered;
  if (mode === 'pre_appointment') {
    const lead = imminentItem(now, horizon);
    const rest = opportunities.filter((o) => !sameOpportunity(o, lead)).sort(byScore);
    ordered = lead ? [lead, ...rest] : rest;
  } else if (mode === 'morning') {
    ordered = [...opportunities].sort(byDeadlineThenScore);
  } else {
    ordered = [...opportunities].sort(byScore);
  }

  return {
    mode,
    items: ordered.slice(0, HOME_ITEM_LIMIT),
    remaining: Math.max(0, ordered.length - HOME_ITEM_LIMIT),
    handledExpanded: mode === 'evening',
    showDecisions: mode !== 'evening',
    // Pre-appointment: everything but the lead collapses so the one thing
    // that is about to happen is the one thing on screen.
    collapseRest: mode === 'pre_appointment',
  };
}

function sameOpportunity(a, b) {
  if (!a || !b) return false;
  return a.kind === b.kind && String(a.subject_id) === String(b.subject_id);
}
