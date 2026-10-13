#!/usr/bin/env node
/**
 * The route / component / API / role matrix (prompt 02 T02.01–T02.03).
 *
 *   node scripts/build-all-pages-matrix.mjs          # rewrite the matrix
 *   node scripts/build-all-pages-matrix.mjs --check  # exit 1 if it is stale
 *
 * Every page comes from a registry the app itself uses, never from the top
 * navigation: routes.js (views, Work types, entity sheets, legacy paths, Sales
 * routes), App.jsx (routes outside the signed-in shell), OurAITab (AI hub
 * workspaces), SalesWorkspace, DealsTab, PropertiesTab and CrmShell (profile
 * views). src/allPagesMatrix.test.js fails when a registry gains a page the
 * matrix lacks, or the matrix keeps a page the registries no longer have.
 *
 * API calls are the direct calls in each page's own component file
 * (crm* / api* helpers and fetch); calls made inside hooks or child
 * components are not followed. Verdicts start at `not_inspected` and are set
 * per row as pages are verified (T02.09, AC02.09).
 */
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = resolve(HERE, '..');
const SRC = join(APP, 'src');
const REPO = resolve(APP, '..');
const OUT_JSON = join(REPO, 'docs/claude-prompts/all-pages.json');
const OUT_MD = join(REPO, 'docs/claude-prompts/ALL-PAGES-MATRIX.md');
// Hand-edited: { "<page id>": { "verdict": "pass|fail|setup_required", "evidence": "...", "reviewer": "...", "date": "YYYY-MM-DD" } }
// Kept apart so regenerating the matrix never erases a reviewed verdict (AC02.09).
const VERDICTS = join(REPO, 'docs/claude-prompts/all-pages-verdicts.json');
const VERDICT_VALUES = new Set(['pass', 'fail', 'setup_required']);

function loadVerdicts() {
  if (!existsSync(VERDICTS)) return {};
  const raw = JSON.parse(readFileSync(VERDICTS, 'utf8'));
  for (const [id, v] of Object.entries(raw)) {
    if (id.startsWith('//')) continue;
    if (!VERDICT_VALUES.has(v?.verdict) || !v.evidence || !v.reviewer || !v.date) {
      throw new Error(`all-pages-verdicts.json: ${id} needs verdict (pass|fail|setup_required), evidence, reviewer and date`);
    }
  }
  return raw;
}

const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

/** Entries of `const NAME = [ { id: 'x', label: 'Y', … }, … ]` (or path:). */
function arrayEntries(source, constName, key = 'id') {
  const start = source.indexOf(`const ${constName} = [`);
  if (start < 0) throw new Error(`registry ${constName} not found`);
  const body = source.slice(start, source.indexOf('];', start));
  const re = new RegExp(`\\{\\s*${key}:\\s*'([^']+)',\\s*label:\\s*'([^']+)'(?:[^}]*Component:\\s*([A-Za-z]+))?`, 'g');
  return [...body.matchAll(re)].map((m) => ({ id: m[1], label: m[2], component: m[3] || null }));
}

/** `{view === 'id' && <Comp` / `{view === 'id' ? <Comp` mappings in a file. */
function viewComponents(source) {
  const out = {};
  for (const m of source.matchAll(/view === '([^']+)'\s*(?:&&|\?)\s*<([A-Z][A-Za-z]+)/g)) out[m[1]] = m[2];
  return out;
}

let fileIndex = null;
function componentFile(name) {
  if (!name) return null;
  if (!fileIndex) {
    fileIndex = new Map();
    const walk = (dir) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (/\.(jsx|tsx|js)$/.test(entry.name) && !/\.test\./.test(entry.name)) {
          const base = entry.name.replace(/\.(jsx|tsx|js)$/, '');
          if (!fileIndex.has(base)) fileIndex.set(base, relative(APP, full));
        }
      }
    };
    walk(SRC);
  }
  return fileIndex.get(name) || null;
}

const CALL = /\b(crmGet|crmPost|crmPut|crmPatch|crmDelete|crmUpload|apiGet|apiPost|apiPut|apiPatch|apiDelete|fetch)\(\s*(['"`])([^'"`]+)\2/g;
const METHOD = { crmGet: 'GET', apiGet: 'GET', fetch: 'GET', crmPost: 'POST', apiPost: 'POST', crmUpload: 'POST',
  crmPut: 'PUT', apiPut: 'PUT', crmPatch: 'PATCH', apiPatch: 'PATCH', crmDelete: 'DELETE', apiDelete: 'DELETE' };

function apiCalls(file) {
  if (!file) return [];
  const source = readFileSync(join(APP, file), 'utf8');
  const calls = new Set();
  for (const m of source.matchAll(CALL)) {
    // A leading `${API_BASE}` (or any base expression) is the origin, not the path.
    let path = m[3].replace(/^\$\{[^}]*\}/, '').replace(/\$\{[^}]*\}/g, '{param}').split('?')[0];
    if (!/^\/(api|billing|portal|auth)\//.test(path)) continue;
    path = path.replace(/\{param\}(\{param\})+/g, '{param}');
    let method = METHOD[m[1]];
    if (m[1] === 'fetch') {
      // fetch(url, { method: 'POST', … }): read the options that follow the URL.
      const after = source.slice(m.index + m[0].length, m.index + m[0].length + 240);
      const opt = /^\s*,\s*\{[^)]*?method:\s*['"`]([A-Z]+)['"`]/.exec(after);
      if (opt) method = opt[1];
    }
    calls.add(`${method} ${path}`);
  }
  return [...calls].sort();
}

// The AI hub renders its workspaces inline (OurAITab.jsx, the
// activeWorkspace.id === '…' blocks), so the mapping is declared here. The
// matrix test fails if a workspace appears without one.
const AI_WORKSPACE_COMPONENT = {
  intelligence: 'IntelligenceFeed',
  autonomy: 'AutonomyControls',
  cowork: 'PersonalAITab',
  sales: 'SalesWorkspace',
  social: 'OurAITab',
  homeowners: 'OurAITab',
  automations: 'OurAITab',
  sites: 'StudioTab',
};

// Routes outside the signed-in shell (App.jsx). Who may open each one.
const PUBLIC_ROUTES = [
  { id: 'public:reel', marker: "'/reel'", path: '/reel', label: 'Reel', component: 'ReelExperience', roles: ['anonymous'], gate: 'none' },
  { id: 'public:site-preview', marker: "'/site-preview/'", path: '/site-preview/{slug}', label: 'Private site preview', component: 'SitePreview', roles: ['member'], gate: 'signed-in session (calls the CRM API)' },
  { id: 'public:sites', marker: "'/sites/'", path: '/sites/{brokerage}/{site}', label: 'Published site', component: 'PublicSite', roles: ['anonymous'], gate: 'published sites only' },
  { id: 'public:property-upload', marker: "'/property-upload/'", path: '/property-upload/{token}', label: 'Client property upload', component: 'PropertyUploadPage', roles: ['anonymous'], gate: 'capability token' },
  { id: 'public:secure-dossier', marker: "'/vault/secure-access/'", path: '/vault/secure-access/{token}', label: 'Client dossier', component: 'SecureDossierPage', roles: ['client'], gate: 'capability token' },
  { id: 'public:accept-invite', marker: "'/accept-invite'", path: '/accept-invite', label: 'Accept invitation', component: 'AcceptInvitePage', roles: ['invitee'], gate: 'invitation token' },
];

const WORK_TYPE_COMPONENT = {
  recent: 'UniversalWorkspace', people: 'PeopleTab', properties: 'PropertiesTab', deals: 'DealsTab',
  conversations: 'CommsTab', opportunities: 'IntelligenceFeed', missions: 'MissionBuilder',
  ai: 'OurAITab', sales: 'OurAITab', social: 'OurAITab', homeowners: 'OurAITab',
  automations: 'OurAITab', sites: 'OurAITab',
};

const MEMBER = ['member'];
const MEMBER_GATE = 'signed-in member; per-role server checks not yet inspected (prompt 03, T02.14)';

export async function buildMatrix() {
  const routes = await import(pathToFileURL(join(SRC, 'routes.js')).href);
  const appSrc = read('App.jsx');
  const ourAi = read('components/OurAITab.jsx');
  const sales = read('components/SalesWorkspace.jsx');
  const deals = read('components/DealsTab.jsx');
  const props = read('components/PropertiesTab.jsx');
  const shell = read('components/CrmShell.jsx');

  const verdicts = loadVerdicts();
  const aliasesFor = (target) => Object.entries(routes.LEGACY_PATHS)
    .filter(([, to]) => to === target).map(([from]) => from).sort();
  const rows = [];
  const add = (row) => {
    const component = row.component || null;
    const file = componentFile(component);
    rows.push({
      id: row.id, family: row.family, label: row.label, path: row.path,
      aliases: row.aliases || aliasesFor(row.path), component, component_file: file,
      registry: row.registry, roles: row.roles || MEMBER, gate: row.gate || MEMBER_GATE,
      apis: apiCalls(file), legacy: row.legacy || null,
      verdict: verdicts[row.id]?.verdict || 'not_inspected',
      verdict_evidence: verdicts[row.id]?.evidence || null,
    });
  };

  for (const [view, path] of Object.entries(routes.VIEW_PATHS)) {
    add({ id: `view:${view}`, family: 'Top-level views', label: view[0].toUpperCase() + view.slice(1), path,
      component: { home: 'NeohHome', work: 'UniversalWorkspace', neoh: 'NeohConversation' }[view],
      registry: 'src/routes.js VIEW_PATHS' });
  }
  for (const type of routes.WORK_TYPES) {
    add({ id: `work:${type}`, family: 'Work types', label: type, path: `/work?type=${type}`,
      component: WORK_TYPE_COMPONENT[type], registry: 'src/routes.js WORK_TYPES' });
  }
  for (const [kind, path] of [['person', '/p/{id}'], ['property', '/property/{key}'], ['deal', '/deal/{id}']]) {
    add({ id: `entity:${kind}`, family: 'Entity sheets', label: `${kind} sheet`, path,
      component: 'EntitySheet', registry: 'src/routes.js ENTITY_PREFIXES' });
  }
  for (const ws of arrayEntries(ourAi, 'WORKSPACES')) {
    add({ id: `ai:${ws.id}`, family: 'AI hub workspaces', label: ws.label, path: `/work?type=ai (workspace: ${ws.id})`,
      aliases: [], component: AI_WORKSPACE_COMPONENT[ws.id] || null, registry: 'src/components/OurAITab.jsx WORKSPACES' });
  }
  for (const dest of arrayEntries(sales, 'DESTINATIONS', 'path')) {
    const path = `/work?type=sales&sales=${encodeURIComponent(dest.id)}`;
    add({ id: `sales:${dest.id.split('/').pop()}`, family: 'Sales pages', label: dest.label, path,
      aliases: aliasesFor(path), component: dest.component, registry: 'src/components/SalesWorkspace.jsx DESTINATIONS' });
  }
  const dealComponents = viewComponents(deals);
  for (const v of arrayEntries(deals, 'VIEWS')) {
    add({ id: `deals:${v.id}`, family: 'Deals sub-views', label: v.label, path: `/work?type=deals (view: ${v.id})`,
      aliases: [], component: dealComponents[v.id] || null, registry: 'src/components/DealsTab.jsx VIEWS' });
  }
  const propertyComponents = viewComponents(props);
  for (const v of arrayEntries(props, 'VIEWS')) {
    add({ id: `properties:${v.id}`, family: 'Properties sub-views', label: v.label,
      path: `/work?type=properties (view: ${v.id})`, aliases: [], component: propertyComponents[v.id] || null,
      registry: 'src/components/PropertiesTab.jsx VIEWS',
      legacy: v.id === 'mls' ? 'MLS is out of scope (D-2026-10-12-01); removed by prompt 11 T11.01' : null });
  }
  const profileStart = shell.indexOf('const profileViews = isPlatformAdmin');
  const profileBlock = shell.slice(profileStart, shell.indexOf(';', shell.indexOf(']', shell.indexOf(': [', profileStart) + 4)));
  const seenProfile = new Set();
  for (const m of profileBlock.matchAll(/\{\s*id:\s*'([^']+)',\s*label:\s*'([^']+)'[^}]*Component:\s*([A-Za-z]+)/g)) {
    if (seenProfile.has(m[1])) continue;
    seenProfile.add(m[1]);
    const adminOnly = m[1] === 'ops';
    add({ id: `profile:${m[1]}`, family: 'Profile views', label: m[2], path: `profile menu (view: ${m[1]})`,
      aliases: [], component: m[3], registry: 'src/components/CrmShell.jsx profileViews',
      roles: adminOnly ? ['platform_admin'] : MEMBER,
      gate: adminOnly ? 'client-side check of sessionStorage role (CrmShell isPlatformAdmin); server check is T02.14' : MEMBER_GATE });
  }
  for (const route of PUBLIC_ROUTES) {
    if (!appSrc.includes(route.marker)) continue;   // a removed route drops out, and the test notices
    add({ ...route, family: 'Outside the signed-in shell', aliases: [], registry: 'src/App.jsx' });
  }
  const unknown = Object.keys(verdicts).filter((id) => !id.startsWith('//') && !rows.some((r) => r.id === id));
  if (unknown.length) throw new Error(`all-pages-verdicts.json names pages that do not exist: ${unknown.join(', ')}`);
  return rows;
}

/** Every page id the registries declare, for the coverage test. */
export async function registryIds() {
  return (await buildMatrix()).map((row) => row.id);
}

function markdown(rows) {
  const families = [...new Set(rows.map((r) => r.family))];
  const esc = (s) => String(s ?? '').replace(/\|/g, '\\|');
  const out = [
    '# All-pages matrix',
    '',
    '> Generated by `oracle-app/scripts/build-all-pages-matrix.mjs` from the app\'s own registries (prompt 02 T02.01). Do not edit by hand: run `npm run pages:matrix` in `oracle-app/`. `src/allPagesMatrix.test.js` fails when a registry and this matrix disagree.',
    '>',
    '> **APIs** are the direct calls in each page\'s own component file; calls inside hooks or child components are not followed. **Verdict** starts at `not_inspected` and is set per row as each page is verified (T02.09). Status lives in [LEDGER.md](LEDGER.md#l02).',
    '',
    `${rows.length} pages across ${families.length} registries.`,
    '',
  ];
  for (const family of families) {
    const group = rows.filter((r) => r.family === family);
    out.push(`## ${family} (${group.length})`, '', '| Page | Path | Component | Roles · gate | APIs (direct) | Verdict |', '|---|---|---|---|---|---|');
    for (const r of group) {
      const page = `**${esc(r.label)}**<br>\`${esc(r.id)}\`${r.legacy ? `<br>*${esc(r.legacy)}*` : ''}`;
      const path = `\`${esc(r.path)}\`${r.aliases.length ? `<br>aliases: ${r.aliases.map((a) => `\`${esc(a)}\``).join(', ')}` : ''}`;
      const comp = r.component_file ? `\`${esc(r.component)}\`<br>\`${esc(r.component_file)}\`` : `\`${esc(r.component)}\` (file not found)`;
      const apis = r.apis.length ? r.apis.map((a) => `\`${esc(a)}\``).join('<br>') : '—';
      out.push(`| ${page} | ${path} | ${comp} | ${esc(r.roles.join(', '))} · ${esc(r.gate)} | ${apis} | \`${r.verdict}\` |`);
    }
    out.push('');
  }
  return out.join('\n');
}

async function main() {
  const rows = await buildMatrix();
  const json = `${JSON.stringify({ generated_by: 'oracle-app/scripts/build-all-pages-matrix.mjs', pages: rows }, null, 2)}\n`;
  const md = `${markdown(rows)}\n`;
  if (process.argv.includes('--check')) {
    const stale = (!existsSync(OUT_JSON) || readFileSync(OUT_JSON, 'utf8') !== json)
      || (!existsSync(OUT_MD) || readFileSync(OUT_MD, 'utf8') !== md);
    if (stale) {
      console.error('ALL-PAGES-MATRIX is stale: run `npm run pages:matrix` in oracle-app/.');
      process.exit(1);
    }
    console.log(`ALL-PAGES-MATRIX current: ${rows.length} pages.`);
    return;
  }
  writeFileSync(OUT_JSON, json);
  writeFileSync(OUT_MD, md);
  console.log(`Wrote ${rows.length} pages to ${relative(REPO, OUT_MD)} and ${relative(REPO, OUT_JSON)}.`);
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) await main();
