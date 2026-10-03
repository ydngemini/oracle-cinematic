/**
 * Customer-facing integration state — one vocabulary for every place a
 * brokerage sees whether Phone, Messaging, MLS, Email or Calendar works.
 *
 *   integrationStatus('mls', 'IN_PROGRESS') → { label: 'Syncing', tone: 'progress' }
 *   integrationStatus('calendar', 'ERROR')  → { label: 'Reconnect', tone: 'attention' }
 *
 * Raw provider / API states (validation_status, NEEDS_ACTION, "setup_required",
 * a vendor's own error code) never reach the screen; anything unrecognised
 * reads as "Needs attention" rather than leaking the code.
 *
 * Tones: 'ready' | 'progress' | 'attention' | 'problem' | 'idle'. The tone picks
 * an icon AND a colour in IntegrationStatus, so state is never colour-only.
 */

const GENERIC = {
  READY: { label: 'Ready', tone: 'ready' },
  NOT_STARTED: { label: 'Not set up', tone: 'idle' },
  OPTIONAL: { label: 'Optional', tone: 'idle' },
  NEEDS_ACTION: { label: 'Needs you', tone: 'attention' },
  IN_PROGRESS: { label: 'In progress', tone: 'progress' },
  BLOCKED: { label: 'Waiting on setup', tone: 'attention' },
  ERROR: { label: 'Needs attention', tone: 'problem' },
};

// Per-integration wording, where a specific verb tells the person what to do.
const OVERRIDES = {
  phone: {
    NEEDS_ACTION: { label: 'Needs verification', tone: 'attention' },
    IN_PROGRESS: { label: 'Verifying', tone: 'progress' },
    ERROR: { label: 'Unavailable', tone: 'problem' },
  },
  messaging: {
    NEEDS_ACTION: { label: 'Needs verification', tone: 'attention' },
    IN_PROGRESS: { label: 'Verifying', tone: 'progress' },
    ERROR: { label: 'Disconnected', tone: 'problem' },
  },
  mls: {
    IN_PROGRESS: { label: 'Syncing', tone: 'progress' },
    NEEDS_ACTION: { label: 'Needs your MLS login', tone: 'attention' },
    ERROR: { label: 'Sync paused', tone: 'problem' },
  },
  email: {
    NEEDS_ACTION: { label: 'Connect', tone: 'attention' },
    ERROR: { label: 'Reconnect', tone: 'problem' },
  },
  calendar: {
    NEEDS_ACTION: { label: 'Connect', tone: 'attention' },
    ERROR: { label: 'Reconnect', tone: 'problem' },
  },
  email_calendar: {
    NEEDS_ACTION: { label: 'Connect', tone: 'attention' },
    ERROR: { label: 'Reconnect', tone: 'problem' },
  },
  billing: {
    NEEDS_ACTION: { label: 'Add payment details', tone: 'attention' },
    ERROR: { label: 'Payment problem', tone: 'problem' },
  },
};

// Provider-level validation states (ProviderDeliveryPage's connection rows)
// folded into the same five canonical states.
const RAW_TO_CANONICAL = {
  valid: 'READY',
  connected: 'READY',
  ready: 'READY',
  active: 'READY',
  ok: 'READY',
  healthy: 'READY',
  pending: 'IN_PROGRESS',
  validating: 'IN_PROGRESS',
  verifying: 'IN_PROGRESS',
  syncing: 'IN_PROGRESS',
  in_progress: 'IN_PROGRESS',
  unvalidated: 'NEEDS_ACTION',
  unverified: 'NEEDS_ACTION',
  needs_verification: 'NEEDS_ACTION',
  needs_action: 'NEEDS_ACTION',
  setup_required: 'NOT_STARTED',
  not_configured: 'NOT_STARTED',
  not_started: 'NOT_STARTED',
  missing: 'NOT_STARTED',
  invalid: 'ERROR',
  failed: 'ERROR',
  error: 'ERROR',
  expired: 'ERROR',
  revoked: 'ERROR',
  disconnected: 'ERROR',
  blocked: 'BLOCKED',
  optional: 'OPTIONAL',
};

export function canonicalState(raw) {
  if (raw == null || raw === '') return 'NOT_STARTED';
  const text = String(raw);
  if (GENERIC[text]) return text;
  return RAW_TO_CANONICAL[text.toLowerCase()] || 'ERROR';
}

export function integrationStatus(integration, rawState) {
  const state = canonicalState(rawState);
  const override = OVERRIDES[integration]?.[state];
  return { state, ...(override || GENERIC[state]) };
}
