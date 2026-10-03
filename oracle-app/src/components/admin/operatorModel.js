// Pure presentation model for the operator overview. No React, no fetches —
// every rule that decides what an operator is told lives here so it can be
// tested without rendering.

// What an operator looks at first, in priority order (mission §13). Each feed
// resolves on its own: one failing endpoint degrades one card, never the page.
export const OPERATOR_ENDPOINTS = {
  health: '/api/admin/health/components',
  release: '/api/admin/release',
  brokerages: '/api/admin/brokerages?limit=100',
  billing: '/api/admin/billing/exceptions',
  mls: '/api/admin/mls/feeds',
  comms: '/api/admin/comms',
  ai: '/api/admin/ai',
  security: '/api/admin/anomalies?limit=20',
  pilot: '/api/admin/pilot-metrics?days=7',
};

// Every state is shown as words; the tone only adds colour and a glyph shape,
// so nothing depends on colour alone.
const TONES = {
  ok: { glyph: '●', label: 'OK' },
  warn: { glyph: '▲', label: 'Attention' },
  bad: { glyph: '■', label: 'Problem' },
  muted: { glyph: '○', label: 'Not used' },
};

export function toneGlyph(tone) {
  return (TONES[tone] || TONES.muted).glyph;
}

const STATE_TONE = {
  // component_health
  HEALTHY: 'ok', NOT_CONFIGURED: 'muted', UNKNOWN: 'warn',
  DEGRADED: 'warn', RECOVERING: 'warn', STALE: 'warn', RATE_LIMITED: 'warn',
  UNAVAILABLE: 'bad', AUTH_FAILED: 'bad',
  // operator capabilities
  READY: 'ok', NEEDS_SETUP: 'warn', NEEDS_ATTENTION: 'bad', NOT_USED: 'muted',
  // MLS feed health
  CONFIGURED: 'warn', BACKFILLING: 'warn', ERROR: 'bad', AUTH_ERROR: 'bad',
};

export function toneOf(state) {
  return STATE_TONE[state] || 'warn';
}

const STATE_WORDS = {
  HEALTHY: 'Healthy', DEGRADED: 'Degraded', UNAVAILABLE: 'Unavailable', UNKNOWN: 'Unknown',
  RECOVERING: 'Recovering', STALE: 'Stale', RATE_LIMITED: 'Rate limited', AUTH_FAILED: 'Credentials rejected',
  NOT_CONFIGURED: 'Not configured',
  READY: 'Ready', NEEDS_SETUP: 'Needs setup', NEEDS_ATTENTION: 'Needs attention', NOT_USED: 'Not used',
  CONFIGURED: 'Configured', BACKFILLING: 'Backfilling', ERROR: 'Failing', AUTH_ERROR: 'Credentials rejected',
};

export function stateWords(state) {
  if (!state) return 'Unknown';
  return STATE_WORDS[state] || String(state).replace(/_/g, ' ').toLowerCase();
}

export const CAPABILITY_LABELS = {
  phone: 'Phone', messaging: 'Texting', email: 'Email', calendar: 'Calendar', mls: 'MLS', billing: 'Billing',
};

const COMPONENT_LABELS = {
  database: 'Database', realtime_fanout: 'Live updates', valkey: 'Valkey', workers: 'Background workers',
  scheduler: 'Scheduler', job_queue: 'Job queue', mls: 'MLS feeds', ai: 'Neoh AI',
  outbound_side_effects: 'Calls, texts & emails', email_outbox: 'Email outbox',
};

export function componentLabel(name) {
  return COMPONENT_LABELS[name] || String(name).replace(/_/g, ' ');
}

export const WORK_COMPONENTS = ['workers', 'scheduler', 'job_queue', 'outbound_side_effects', 'email_outbox'];

/** Components that are not fine, worst first. */
export function componentProblems(health) {
  const components = health?.components || {};
  const rank = { bad: 0, warn: 1 };
  return Object.entries(components)
    .filter(([, c]) => !['HEALTHY', 'NOT_CONFIGURED'].includes(c?.state))
    .map(([name, c]) => ({ name, label: componentLabel(name), state: c?.state, summary: c?.summary || '' }))
    .sort((a, b) => (rank[toneOf(a.state)] ?? 2) - (rank[toneOf(b.state)] ?? 2));
}

export function shortSha(sha) {
  if (!sha || sha === 'unknown') return 'unknown';
  return String(sha).slice(0, 7);
}

/** One line per provider family that is degraded, in product language. */
export function providerDegradation({ ai, mls, comms } = {}) {
  const out = [];
  if (ai) {
    const health = ai.health?.state;
    if (health && !['HEALTHY', 'NOT_CONFIGURED'].includes(health)) {
      out.push({ key: 'ai-health', tone: toneOf(health), text: `Neoh AI: ${stateWords(health).toLowerCase()} — ${ai.health?.summary || ''}`.trim() });
    }
    if (ai.gateway && ai.gateway.configured === false) {
      out.push({ key: 'ai-none', tone: 'bad', text: 'No AI provider is configured' });
    }
    Object.entries(ai.gateway?.providers || {}).forEach(([name, p]) => {
      if (p.rate_limited > 0) out.push({ key: `ai-rl-${name}`, tone: 'warn', text: `${name} is rate limiting (${p.rate_limited} on this replica)` });
      else if (p.failure_rate !== null && p.failure_rate >= 0.2 && p.failures >= 3) {
        out.push({ key: `ai-fail-${name}`, tone: 'warn', text: `${name} failing ${Math.round(p.failure_rate * 100)}% of calls` });
      }
    });
    const tools = ai.tools_24h;
    if (tools?.failure_rate !== null && tools?.failure_rate >= 0.2 && tools?.failed >= 3) {
      out.push({ key: 'ai-tools', tone: 'warn', text: `Neoh actions failing ${Math.round(tools.failure_rate * 100)}% (24h)` });
    }
  }
  (mls?.feeds || []).forEach((f) => {
    if (f.licensed && f.health !== 'READY') {
      out.push({ key: `mls-${f.mls_id}`, tone: toneOf(f.health), text: `MLS ${f.mls_name}: ${stateWords(f.health).toLowerCase()}` });
    }
  });
  const troubled = (comms?.brokerages || []).filter((b) => (b.problems || []).length > 0);
  if (troubled.length) {
    out.push({
      key: 'comms',
      tone: 'warn',
      text: `Calls/texts: ${troubled.length} brokerage${troubled.length === 1 ? '' : 's'} with problems (${troubled[0].name || troubled[0].tenant_id}: ${troubled[0].problems[0]})`,
    });
  }
  return out;
}

const STATUS_WORDS = {
  live: 'Live', onboarding: 'Onboarding', canceled: 'Canceled', suspended: 'Suspended',
  closing: 'Closing', erasing: 'Erasing', erased: 'Erased', platform: 'Platform',
};

export function statusWords(status) {
  return STATUS_WORDS[status] || String(status || 'unknown');
}

export function relTime(input, now = Date.now()) {
  if (input === null || input === undefined || input === '') return null;
  const ms = typeof input === 'number' ? (input > 1e12 ? input : input * 1000) : new Date(input).getTime();
  if (Number.isNaN(ms)) return null;
  const diff = now - ms;
  const s = Math.round(Math.abs(diff) / 1000);
  let label;
  if (s < 45) label = `${s}s`;
  else if (s < 2700) label = `${Math.max(1, Math.round(s / 60))}m`;
  else if (s < 129600) label = `${Math.round(s / 3600)}h`;
  else label = `${Math.round(s / 86400)}d`;
  return diff < 0 ? `in ${label}` : `${label} ago`;
}

const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

export function fmtMoney(v) {
  const n = Number(v);
  return Number.isFinite(n) ? money.format(n) : '—';
}
