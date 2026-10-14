import { describe, expect, it } from 'vitest';
import { factText, footprintLookupView, isFailed, reasonText } from './sourceStatus';

describe('factText: a blank next to a failed source is not "none" (A02)', () => {
  const fmt = () => '42';

  it.each([
    [{ status: 'timed_out', reason: 'upstream_timeout' }, "Couldn't check"],
    [{ status: 'unavailable', reason: 'upstream_error' }, "Couldn't check"],
    [{ status: 'unavailable', reason: 'not_configured' }, 'Not configured'],
  ])('failed source %o reads %s even if a value slipped through', (entry, text) => {
    expect(factText(entry, false, fmt)).toBe(text);
    expect(factText(entry, true, fmt)).toBe(text);
  });

  it('a checked empty source is a verified none', () => {
    expect(factText({ status: 'checked' }, false, fmt, 'No score')).toBe('No score');
  });

  it('a value renders; no status at all is "Not checked"', () => {
    expect(factText({ status: 'checked' }, true, fmt)).toBe('42');
    expect(factText(undefined, false, fmt)).toBe('Not checked');
  });

  it('isFailed / reasonText', () => {
    expect(isFailed({ status: 'not_applicable' })).toBe(false);
    expect(isFailed(null)).toBe(false);
    expect(reasonText({ status: 'timed_out' })).toBe('timed out');
    expect(reasonText({ status: 'unavailable', reason: 'credential_expired' })).toBe('credential expired');
  });
});

describe('footprintLookupView', () => {
  it('found renders nothing', () => {
    expect(footprintLookupView('found', {})).toBeNull();
  });

  it('not_checked names what could not be asked and is not a "no footprint"', () => {
    const view = footprintLookupView('not_checked', {
      regrid: { status: 'unavailable', reason: 'credential_expired' },
      openstreetmap: { status: 'not_applicable', reason: 'no_coordinates' },
    });
    expect(view.failed).toBe(true);
    expect(view.text).toMatch(/Couldn't check/);
    expect(view.text).toMatch(/licensed parcel data credential expired/);
    expect(view.text).toMatch(/OpenStreetMap needs map coordinates/);
    expect(view.text).not.toMatch(/No building outline found/);
  });

  it('incomplete is not a "no footprint" either', () => {
    const view = footprintLookupView('incomplete', {
      regrid: { status: 'timed_out', reason: 'upstream_timeout' },
      openstreetmap: { status: 'checked' },
    });
    expect(view.failed).toBe(true);
    expect(view.text).toMatch(/not every source could be checked/);
  });

  it('none_found (or an older backend without lookup) keeps the verified-miss wording', () => {
    expect(footprintLookupView('none_found', {}).text).toMatch(/No building outline found/);
    expect(footprintLookupView(undefined, undefined).failed).toBe(false);
  });
});
