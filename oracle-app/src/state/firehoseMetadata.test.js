import { describe, expect, it } from 'vitest';
import { firehoseMetadataEntry, shortTenantId } from './firehoseMetadata';

const TENANT = '11111111-2222-4333-8444-555555555555';

describe('firehoseMetadataEntry', () => {
  it('keeps the frame type, brokerage, time and flags of a metadata frame', () => {
    const entry = firehoseMetadataEntry({
      type: 'FIREHOSE_METADATA', version: 1, frame_type: 'VOICE_TELEMETRY', source_tenant: TENANT,
      content_withheld: true, mirrored_at: '2026-11-21T10:00:00+00:00', consent_recorded: true,
      requires_agent_approval: true, transcript_status: 'active', mao_unavailable: true,
      extraction_code: 'AI_BUDGET_EXHAUSTED',
    });
    expect(entry).toEqual({
      kind: 'firehose_metadata', frameType: 'VOICE_TELEMETRY', tenant: TENANT,
      flags: ['consent recorded', 'needs approval', 'transcript active', 'MAO unavailable', 'ai budget exhausted'],
      timestamp: Date.parse('2026-11-21T10:00:00+00:00'),
    });
  });

  it('copies nothing else a frame carries, and re-checks every value', () => {
    const entry = firehoseMetadataEntry({
      type: 'FIREHOSE_METADATA', frame_type: 'VOICE_TELEMETRY Pat Owner', source_tenant: 'Pat Owner 12 Elm',
      transcript: { text: 'Pat Owner: 251357 is my number' }, counter_offer: 251357, mao: 233911,
      consent_recorded: 'Pat said yes', transcript_status: 'active: Pat', extraction_code: 'call 302-555-0147',
      mirrored_at: 'Pat Owner',
    });
    expect(entry).toEqual({ kind: 'firehose_metadata', frameType: null, tenant: null, flags: [] });
    expect(JSON.stringify(entry)).not.toMatch(/Pat|251357|233911|302-555/);
    expect(firehoseMetadataEntry(null)).toEqual({ kind: 'firehose_metadata', frameType: null, tenant: null, flags: [] });
  });

  it('says when consent was not recorded', () => {
    expect(firehoseMetadataEntry({ frame_type: 'CALL_CONSENT', consent_recorded: false }).flags).toEqual(['no consent']);
  });

  it('shortens a brokerage id to its first block', () => {
    expect(shortTenantId(TENANT)).toBe('11111111');
    expect(shortTenantId(undefined)).toBe('');
  });
});
