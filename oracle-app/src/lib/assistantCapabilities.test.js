import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { CAPABILITY_SNAPSHOT, unbackedClaims } from './assistantCapabilities';

// Audit A09 (ledger L07-01): UI copy that describes or asks for an assistant
// capability must agree with the tools the assistant can actually run. The
// registry is backend/ai_capabilities.py; this snapshot is generated from it.

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative) => readFileSync(resolve(here, relative), 'utf8');

// Everything sent to the chat model from a chip in the Neoh tools workspace.
function chatPrompts(source) {
  return [...source.matchAll(/prompt: '((?:[^'\\]|\\.)*)'/g)].map((match) => match[1]);
}

describe('assistant capability snapshot', () => {
  it('classifies every area into one of the five states', () => {
    const states = new Set(['available', 'approval_gated', 'degraded', 'setup_required', 'unavailable']);
    expect(CAPABILITY_SNAPSHOT.areas.length).toBeGreaterThan(10);
    for (const area of CAPABILITY_SNAPSHOT.areas) expect(states.has(area.state)).toBe(true);
  });

  it('lists public records as not available yet (no executable tool)', () => {
    const area = CAPABILITY_SNAPSHOT.areas.find((entry) => entry.id === 'public_records');
    expect(area.state).toBe('unavailable');
  });

  it('catches the over-claims the audit found and lets disclaimers through', () => {
    expect(unbackedClaims('Browse, search, and filter MLS-sourced property listings.')).not.toEqual([]);
    expect(unbackedClaims('Identify homeowners who may benefit from an annual property and equity review.')).not.toEqual([]);
    expect(unbackedClaims('Flood zone and liens are not available in chat yet.')).toEqual([]);
  });
});

describe('UI copy agrees with what Neoh can run', () => {
  const ourAi = read('../components/OurAITab.jsx');

  it('finds the chat prompts it checks', () => {
    expect(chatPrompts(ourAi).length).toBeGreaterThanOrEqual(8);
  });

  it.each(chatPrompts(ourAi).map((prompt) => [prompt.slice(0, 48), prompt]))(
    'Neoh tools chip "%s…" asks only for executable capabilities',
    (_, prompt) => {
      expect(unbackedClaims(prompt)).toEqual([]);
    },
  );

  it.each([
    '../components/AssistantMessages.jsx',
    '../components/AssistantCapabilities.jsx',
    '../components/PersonalAITab.jsx',
    '../neoh/NeohConversation.jsx',
  ])('%s describes no unexecutable capability', (path) => {
    expect(unbackedClaims(read(path))).toEqual([]);
  });

  it.each(['../components/ProductTour.jsx', '../components/OurAITab.jsx'])(
    '%s never presents MLS, IDX or RESO (owner decision D-2026-10-12-01)',
    (path) => {
      const mlsOnly = {
        ...CAPABILITY_SNAPSHOT,
        claim_terms: CAPABILITY_SNAPSHOT.claim_terms.filter((term) => /MLS|IDX|RESO/.test(term.pattern)),
      };
      expect(unbackedClaims(read(path), mlsOnly)).toEqual([]);
    },
  );
});
