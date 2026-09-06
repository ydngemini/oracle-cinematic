import { describe, expect, it } from 'vitest';

import {
  DEFAULT_WORK_TYPE,
  LEGACY_PATHS,
  VIEWS,
  entityHref,
  href,
  parse,
  redirectFor,
  resolveLegacyId,
} from './routes';

/**
 * The address space is three views plus entity routes, and every old address
 * and every old tab id still lands somewhere. These tests are the contract
 * CrmShell relies on; if one breaks, a bookmark or a stored session goes to a
 * blank page.
 */

describe('legacy addresses', () => {
  it('every old tab path redirects, and nothing current does', () => {
    expect(redirectFor('/today')).toBe('/');
    expect(redirectFor('/people')).toBe('/work?type=people');
    expect(redirectFor('/inbox')).toBe('/work?type=conversations');
    expect(redirectFor('/deals')).toBe('/work?type=deals');
    expect(redirectFor('/property-view')).toBe('/work?type=properties');
    expect(redirectFor('/our-ai')).toBe('/work?type=ai');
    expect(redirectFor('/')).toBeNull();
    expect(redirectFor('/work')).toBeNull();
    expect(redirectFor('/neoh')).toBeNull();
  });

  it('tolerates a trailing slash, as CrmShell always did', () => {
    expect(redirectFor('/people/')).toBe('/work?type=people');
  });

  it('carries every sales sub-route through the sales param', () => {
    for (const sub of ['agent', 'dialer', 'plans', 'providers', 'routing']) {
      const target = LEGACY_PATHS[`/our-ai/sales/${sub}`];
      const parsed = parse('/work', target.slice(target.indexOf('?')));
      expect(parsed.params.type).toBe('sales');
      expect(parsed.params.sales).toBe(`/our-ai/sales/${sub}`);
    }
  });

  it('resolves every old stored tab id somewhere, and unknown ids go Home', () => {
    expect(resolveLegacyId('today')).toEqual({ view: 'home' });
    expect(resolveLegacyId('studio')).toEqual({ view: 'work', type: 'ai' });
    expect(resolveLegacyId('houses')).toEqual({ view: 'work', type: 'properties' });
    expect(resolveLegacyId('nonsense')).toEqual({ view: 'home' });
    expect(resolveLegacyId(null)).toEqual({ view: 'home' });
    // A current id passes through untouched.
    expect(resolveLegacyId('work')).toEqual({ view: 'work' });
    // A Work type is a destination in its own right — Home's "more in Work"
    // link relies on this.
    expect(resolveLegacyId('opportunities')).toEqual({ view: 'work', type: 'opportunities' });
    expect(resolveLegacyId('missions')).toEqual({ view: 'work', type: 'missions' });
  });
});

describe('parse', () => {
  it('reads the three views', () => {
    expect(parse('/').view).toBe(VIEWS.home);
    expect(parse('/work').view).toBe(VIEWS.work);
    expect(parse('/neoh').view).toBe(VIEWS.neoh);
  });

  it('defaults an unknown work type rather than trusting the URL', () => {
    expect(parse('/work', '?type=anything').params.type).toBe(DEFAULT_WORK_TYPE);
    expect(parse('/work', '?type=deals').params.type).toBe('deals');
  });

  it('drops a sales param it does not recognise', () => {
    expect(parse('/work', '?type=sales&sales=%2Fevil').params.sales).toBeNull();
  });

  it('returns entity independently of view, so a sheet opens over what was beneath', () => {
    const over = parse('/p/abc-123', '', { fallbackView: VIEWS.work });
    expect(over.entity).toEqual({ kind: 'person', id: 'abc-123' });
    expect(over.view).toBe(VIEWS.work);

    const home = parse('/deal/d1');
    expect(home.entity).toEqual({ kind: 'deal', id: 'd1' });
    expect(home.view).toBe(VIEWS.home);
  });

  it('decodes an entity id and ignores anything after the id segment', () => {
    expect(parse('/property/12%2F34/extra').entity).toEqual({ kind: 'property', id: '12/34' });
  });

  it('does not treat a bare prefix as an entity', () => {
    expect(parse('/p/').entity).toBeNull();
    expect(parse('/p').entity).toBeNull();
  });

  it('lands an unknown path on Home rather than throwing', () => {
    expect(parse('/no/such/thing')).toEqual({ view: VIEWS.home, params: {}, entity: null });
  });
});

describe('href', () => {
  it('is canonical: the same intent always produces the same string', () => {
    expect(href(VIEWS.work)).toBe('/work');
    expect(href(VIEWS.work, { type: DEFAULT_WORK_TYPE })).toBe('/work');
    expect(href(VIEWS.work, { type: 'deals' })).toBe('/work?type=deals');
    expect(href(VIEWS.work, { type: 'deals', q: 'main st' })).toBe('/work?type=deals&q=main+st');
    expect(href(VIEWS.home)).toBe('/');
    expect(href(VIEWS.neoh)).toBe('/neoh');
  });

  it('round-trips through parse', () => {
    const address = href(VIEWS.work, { type: 'sales', sales: '/our-ai/sales/dialer' });
    const parsed = parse('/work', address.slice(address.indexOf('?')));
    expect(parsed.params.type).toBe('sales');
    expect(parsed.params.sales).toBe('/our-ai/sales/dialer');
  });

  it('refuses to write a sales param it would not read back', () => {
    expect(href(VIEWS.work, { type: 'sales', sales: '/nope' })).toBe('/work?type=sales');
  });

  it('builds entity addresses and encodes the id', () => {
    expect(entityHref('person', 'abc')).toBe('/p/abc');
    expect(entityHref('property', 'a/b')).toBe('/property/a%2Fb');
    expect(entityHref('unknown', 'x')).toBe('/');
  });
});
