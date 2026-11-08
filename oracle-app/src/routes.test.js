import { describe, expect, it } from 'vitest';

import {
  DEFAULT_WORK_TYPE,
  LEGACY_PATHS,
  LEGACY_TAB_IDS,
  VIEWS,
  WORK_TYPES,
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

  it('keeps every old AI-hub address working now that the Neoh tab is the conversation', () => {
    // The hub moved into Work; each of its workspaces is a Work type.
    expect(redirectFor('/our-ai')).toBe('/work?type=ai');
    for (const type of ['social', 'homeowners', 'automations', 'sites']) {
      const target = redirectFor(`/our-ai/${type}`);
      expect(target).toBe(`/work?type=${type}`);
      expect(parse('/work', target.slice(target.indexOf('?'))).params.type).toBe(type);
    }
    expect(parse('/work', '?type=ai').params.type).toBe('ai');
    // Cowork was the conversation; the Command workspace was Home's briefing.
    expect(redirectFor('/our-ai/cowork')).toBe('/neoh');
    expect(redirectFor('/our-ai/command')).toBe('/');
    expect(redirectFor('/command-center')).toBe('/');
    expect(resolveLegacyId('cowork')).toEqual({ view: 'neoh' });
    expect(resolveLegacyId('command')).toEqual({ view: 'home' });
    expect(resolveLegacyId('our-ai')).toEqual({ view: 'work', type: 'ai' });
    expect(parse('/neoh').view).toBe(VIEWS.neoh);
  });
});

describe('every alias lands on a real destination (prompt 02 AC02.04)', () => {
  const VALID_VIEWS = Object.values(VIEWS);

  it('sends every LEGACY_PATHS entry, with or without a trailing slash, to a current address', () => {
    for (const [from, to] of Object.entries(LEGACY_PATHS)) {
      expect(redirectFor(from), from).toBe(to);
      expect(redirectFor(`${from}/`), `${from}/`).toBe(to);
      const queryAt = to.indexOf('?');
      const pathname = queryAt === -1 ? to : to.slice(0, queryAt);
      const search = queryAt === -1 ? '' : to.slice(queryAt);
      // The target is terminal: no redirect chain or loop.
      expect(redirectFor(pathname), `${from} -> ${to}`).toBeNull();
      const parsed = parse(pathname, search);
      expect(VALID_VIEWS, to).toContain(parsed.view);
      expect(parsed.entity, to).toBeNull();
      if (parsed.view === VIEWS.work) {
        // The type in the address is the type parse resolves, not a fallback.
        expect(parsed.params.type, to).toBe(new URLSearchParams(search).get('type'));
        expect(WORK_TYPES, to).toContain(parsed.params.type);
        const sales = new URLSearchParams(search).get('sales');
        if (sales) expect(parsed.params.sales, to).toBe(sales);
      }
    }
  });

  it('resolves every LEGACY_TAB_IDS entry to a valid view and, for Work, a known type', () => {
    for (const id of Object.keys(LEGACY_TAB_IDS)) {
      const target = resolveLegacyId(id);
      expect(VALID_VIEWS, id).toContain(target.view);
      if (target.type) {
        expect(target.view, id).toBe(VIEWS.work);
        expect(WORK_TYPES, id).toContain(target.type);
      }
    }
  });

  it('does not resolve inherited object keys as tab ids or paths', () => {
    for (const key of ['constructor', 'toString', 'hasOwnProperty', '__proto__']) {
      expect(resolveLegacyId(key), key).toEqual({ view: VIEWS.home });
      expect(redirectFor(`/${key}`), key).toBeNull();
    }
  });

  it('round-trips every Work type through href and parse', () => {
    for (const type of WORK_TYPES) {
      const address = href(VIEWS.work, { type });
      const queryAt = address.indexOf('?');
      const parsed = parse('/work', queryAt === -1 ? '' : address.slice(queryAt));
      expect(parsed.params.type, type).toBe(type);
    }
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

  it('round-trips a property tour without changing the underlying view', () => {
    const url = new URL(entityHref('property', 'a/b', { tour: true }), 'https://neoh.test');
    const route = parse(url.pathname, url.search, { fallbackView: VIEWS.work });
    expect(route.entity).toEqual({ kind: 'property', id: 'a/b', tour: true });
    expect(route.view).toBe(VIEWS.work);
    expect(parse(url.pathname).entity).toEqual({ kind: 'property', id: 'a/b' });
  });

  it('only opens the tour state for a property and a recognised mode', () => {
    expect(parse('/p/person', '?tour=3d').entity).toEqual({ kind: 'person', id: 'person' });
    expect(parse('/property/home', '?tour=anything').entity).toEqual({ kind: 'property', id: 'home' });
    expect(entityHref('deal', 'deal', { tour: true })).toBe('/deal/deal');
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
