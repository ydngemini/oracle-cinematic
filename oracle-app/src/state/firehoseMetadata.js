// The platform OPS firehose's view of call content (backend ws_hub.firehose_metadata,
// ledger L03-21 / L03-22): which kind of frame, which brokerage, when, and
// state flags. Never the transcript, amounts, guidance, names or numbers.
// This turns one FIREHOSE_METADATA frame into a Live Feed entry from an
// allowlist, each value checked again here, so nothing else a frame carries
// reaches the screen.

const CODE = /^[A-Z][A-Z0-9_]{0,63}$/;
const TENANT_ID = /^[0-9a-f-]{8,36}$/i;
const TRANSCRIPT_STATES = new Set(['pending', 'active', 'complete', 'failed', 'deleted']);

export function firehoseMetadataEntry(msg) {
  const frame = msg && typeof msg === 'object' ? msg : {};
  const flags = [];
  if (frame.consent_recorded === true) flags.push('consent recorded');
  if (frame.consent_recorded === false) flags.push('no consent');
  if (frame.requires_agent_approval === true) flags.push('needs approval');
  if (TRANSCRIPT_STATES.has(frame.transcript_status)) flags.push(`transcript ${frame.transcript_status}`);
  if (frame.mao_unavailable === true) flags.push('MAO unavailable');
  if (typeof frame.extraction_code === 'string' && CODE.test(frame.extraction_code)) {
    flags.push(frame.extraction_code.replace(/_/g, ' ').toLowerCase());
  }
  const at = typeof frame.mirrored_at === 'string' ? Date.parse(frame.mirrored_at) : NaN;
  return {
    kind: 'firehose_metadata',
    frameType: typeof frame.frame_type === 'string' && CODE.test(frame.frame_type) ? frame.frame_type : null,
    tenant: typeof frame.source_tenant === 'string' && TENANT_ID.test(frame.source_tenant)
      ? frame.source_tenant
      : null,
    flags,
    ...(Number.isFinite(at) ? { timestamp: at } : {}),
  };
}

// A brokerage id's first block: enough to tell brokerages apart in the feed.
export function shortTenantId(id) {
  return typeof id === 'string' ? id.slice(0, 8) : '';
}
