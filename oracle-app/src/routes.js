/**
 * routes — the whole address space of the authenticated app, as pure functions.
 *
 * There is no router library and this does not add one. It replaces the
 * six-destination tab table in CrmShell with three views and a family of
 * entity routes, and it keeps every old URL and every old tab id working
 * through the same alias mechanism CrmShell already used (LEGACY_TAB_IDS).
 *
 *   /                      Home   — what matters right now
 *   /work[?q=&type=&sales=] Work   — everything, searchable; the old tabs are
 *                                   views here, chosen by ?type
 *   /neoh                  Neoh   — the full-screen conversation
 *   /p/:id  /property/:key  /deal/:id
 *                          an EntitySheet over whichever view is beneath
 *
 * The one mechanic that makes entity routes cheap: `parse` returns `entity`
 * INDEPENDENTLY of `view`. CrmShell keeps the last non-entity view in a ref,
 * so opening /p/:id renders the sheet over Work (or Home) without remounting
 * it, and Back closes the sheet and leaves the view exactly as it was.
 */

export const VIEWS = Object.freeze({ home: 'home', work: 'work', neoh: 'neoh' });

export const VIEW_PATHS = Object.freeze({ home: '/', work: '/work', neoh: '/neoh' });

/** Work views. The first four are searchable kinds; the rest are the old
 *  Our-AI workspaces and Deals/Property sub-views, reachable by ?type. */
export const WORK_TYPES = Object.freeze([
  'people', 'properties', 'deals', 'conversations',
  'opportunities', 'ai', 'sales', 'social', 'homeowners', 'automations', 'sites', 'missions',
]);
export const DEFAULT_WORK_TYPE = 'people';

const ENTITY_PREFIXES = Object.freeze({ person: '/p/', property: '/property/', deal: '/deal/' });

/** Old tab ids → where they live now. Folds in CrmShell's original alias
 *  table (portfolio, houses, clients, pipeline, …) so a session that stored
 *  any of them resumes somewhere sensible. */
export const LEGACY_TAB_IDS = Object.freeze({
  today: { view: 'home' },
  portfolio: { view: 'home' },
  ops: { view: 'home' },
  profile: { view: 'home' },
  people: { view: 'work', type: 'people' },
  clients: { view: 'work', type: 'people' },
  inbox: { view: 'work', type: 'conversations' },
  comms: { view: 'work', type: 'conversations' },
  deals: { view: 'work', type: 'deals' },
  pipeline: { view: 'work', type: 'deals' },
  docs: { view: 'work', type: 'deals' },
  contracts: { view: 'work', type: 'deals' },
  'property-view': { view: 'work', type: 'properties' },
  houses: { view: 'work', type: 'properties' },
  marketplace: { view: 'work', type: 'properties' },
  house: { view: 'work', type: 'properties' },
  'house-profile': { view: 'work', type: 'properties' },
  studio: { view: 'work', type: 'ai' },
  ai: { view: 'work', type: 'ai' },
  'personal-ai': { view: 'work', type: 'ai' },
});

/** Old paths → new paths. Kept as a table rather than derived so a reader can
 *  see every bookmark that still works. */
export const LEGACY_PATHS = Object.freeze({
  '/today': '/',
  '/people': '/work?type=people',
  '/inbox': '/work?type=conversations',
  '/deals': '/work?type=deals',
  '/property-view': '/work?type=properties',
  '/our-ai': '/work?type=ai',
  '/our-ai/sales': '/work?type=sales',
  '/our-ai/sales/agent': '/work?type=sales&sales=%2Four-ai%2Fsales%2Fagent',
  '/our-ai/sales/dialer': '/work?type=sales&sales=%2Four-ai%2Fsales%2Fdialer',
  '/our-ai/sales/plans': '/work?type=sales&sales=%2Four-ai%2Fsales%2Fplans',
  '/our-ai/sales/providers': '/work?type=sales&sales=%2Four-ai%2Fsales%2Fproviders',
  '/our-ai/sales/routing': '/work?type=sales&sales=%2Four-ai%2Fsales%2Frouting',
});

/** The sales sub-routes SalesWorkspace still understands. Carried through the
 *  ?sales= param so the nested router keeps working unchanged. */
export const SALES_ROUTES = Object.freeze(new Set([
  '/our-ai/sales',
  '/our-ai/sales/agent',
  '/our-ai/sales/dialer',
  '/our-ai/sales/plans',
  '/our-ai/sales/providers',
  '/our-ai/sales/routing',
]));

function trimPath(pathname) {
  if (!pathname) return '/';
  const trimmed = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  return trimmed || '/';
}

/** Where an old address should go, or null if the address is already current. */
export function redirectFor(pathname) {
  const path = trimPath(pathname);
  return Object.prototype.hasOwnProperty.call(LEGACY_PATHS, path) ? LEGACY_PATHS[path] : null;
}

/** Resolve a stored or legacy tab id to a {view, type?}. Unknown ids go Home. */
export function resolveLegacyId(id) {
  if (!id) return { view: VIEWS.home };
  if (id === VIEWS.home || id === VIEWS.work || id === VIEWS.neoh) return { view: id };
  return LEGACY_TAB_IDS[id] || { view: VIEWS.home };
}

/**
 * Parse an address into {view, params, entity}.
 *
 * `entity` is set whenever the path is an entity route, and `view` is then
 * whatever `fallbackView` says — the caller supplies the view that was
 * beneath, so the sheet opens over it rather than over a default.
 */
export function parse(pathname, search = '', { fallbackView = VIEWS.home } = {}) {
  const path = trimPath(pathname);
  const params = new URLSearchParams(search || '');

  for (const [kind, prefix] of Object.entries(ENTITY_PREFIXES)) {
    if (path.startsWith(prefix) && path.length > prefix.length) {
      const id = decodeURIComponent(path.slice(prefix.length).split('/')[0]);
      return {
        view: fallbackView,
        params: paramsToObject(params),
        entity: { kind, id },
      };
    }
  }

  if (path === VIEW_PATHS.work) {
    const type = params.get('type');
    const sales = params.get('sales');
    return {
      view: VIEWS.work,
      params: {
        ...paramsToObject(params),
        type: WORK_TYPES.includes(type) ? type : DEFAULT_WORK_TYPE,
        sales: sales && SALES_ROUTES.has(sales) ? sales : null,
      },
      entity: null,
    };
  }
  if (path === VIEW_PATHS.neoh) {
    return { view: VIEWS.neoh, params: paramsToObject(params), entity: null };
  }
  if (path === VIEW_PATHS.home) {
    return { view: VIEWS.home, params: paramsToObject(params), entity: null };
  }
  // Unknown. Not a legacy path (redirectFor handles those before parse is
  // asked) — treat as Home rather than throwing, so a stray link is a soft
  // landing and not a blank page.
  return { view: VIEWS.home, params: {}, entity: null };
}

function paramsToObject(params) {
  const out = {};
  for (const [key, value] of params.entries()) out[key] = value;
  return out;
}

/** Build an address for a view. Only known params are written, in a fixed
 *  order, so two calls with the same intent produce the same string. */
export function href(view, params = {}) {
  if (view === VIEWS.work) {
    const query = new URLSearchParams();
    if (params.type && params.type !== DEFAULT_WORK_TYPE) query.set('type', params.type);
    if (params.q) query.set('q', params.q);
    if (params.sales && SALES_ROUTES.has(params.sales)) query.set('sales', params.sales);
    const qs = query.toString();
    return qs ? `${VIEW_PATHS.work}?${qs}` : VIEW_PATHS.work;
  }
  if (view === VIEWS.neoh) return VIEW_PATHS.neoh;
  return VIEW_PATHS.home;
}

/** Address of an entity sheet. */
export function entityHref(kind, id) {
  const prefix = ENTITY_PREFIXES[kind];
  if (!prefix || !id) return VIEW_PATHS.home;
  return `${prefix}${encodeURIComponent(String(id))}`;
}
