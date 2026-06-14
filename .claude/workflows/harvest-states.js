export const meta = {
  name: 'harvest-states',
  description: 'Build a real property/parcel harvester for each US state that lacks one: discover the open-data portal, write a BaseHarvester subclass, verify it parses live data, then register all working states',
  phases: [
    { title: 'Discover', detail: 'web-research each state portal + sample real columns', model: 'sonnet' },
    { title: 'Generate', detail: 'write the archetype subclass + map_record method', model: 'sonnet' },
    { title: 'Verify', detail: 'run map_record against live data, no DB', model: 'sonnet' },
    { title: 'Register', detail: 'one barrier edit of the shared registries', model: 'sonnet' }
  ]
}

// --------------------------------------------------------------------------- //
// Input + briefing are embedded as a literal (the args channel serialises
// nested objects to a string — known Workflow gotcha). 41 states missing a
// property harvester, with a portal hint where one is on the public record.
// --------------------------------------------------------------------------- //
const DATA = {
  repo: '/media/ydn/SYPHER_CORE2/Oracle',
  backend: '/media/ydn/SYPHER_CORE2/Oracle/backend',
  states: [
    { code: 'AK', name: 'Alaska', hint: null },
    { code: 'AL', name: 'Alabama', hint: null },
    { code: 'AR', name: 'Arkansas', hint: null },
    { code: 'AZ', name: 'Arizona', hint: 'AZGeo / Maricopa County Assessor' },
    { code: 'CA', name: 'California', hint: 'county assessors (no single statewide parcel layer)' },
    { code: 'CO', name: 'Colorado', hint: 'Colorado county assessors' },
    { code: 'DC', name: 'District of Columbia', hint: 'opendata.dc.gov (Computer Assisted Mass Appraisal)' },
    { code: 'FL', name: 'Florida', hint: 'FGIO Florida statewide parcels (FGDL)' },
    { code: 'GA', name: 'Georgia', hint: 'Georgia GIS Clearinghouse / qPublic' },
    { code: 'HI', name: 'Hawaii', hint: null },
    { code: 'IA', name: 'Iowa', hint: null },
    { code: 'ID', name: 'Idaho', hint: null },
    { code: 'IL', name: 'Illinois', hint: 'Cook County + Illinois county GIS' },
    { code: 'IN', name: 'Indiana', hint: null },
    { code: 'KS', name: 'Kansas', hint: null },
    { code: 'KY', name: 'Kentucky', hint: null },
    { code: 'LA', name: 'Louisiana', hint: null },
    { code: 'ME', name: 'Maine', hint: null },
    { code: 'MI', name: 'Michigan', hint: 'Michigan county equalization / GIS' },
    { code: 'MN', name: 'Minnesota', hint: 'MN Geospatial Commons parcels' },
    { code: 'MO', name: 'Missouri', hint: 'Missouri county assessors' },
    { code: 'MS', name: 'Mississippi', hint: null },
    { code: 'MT', name: 'Montana', hint: null },
    { code: 'ND', name: 'North Dakota', hint: null },
    { code: 'NE', name: 'Nebraska', hint: null },
    { code: 'NH', name: 'New Hampshire', hint: null },
    { code: 'NM', name: 'New Mexico', hint: null },
    { code: 'NV', name: 'Nevada', hint: null },
    { code: 'OH', name: 'Ohio', hint: 'Ohio county auditors (CAMA)' },
    { code: 'OK', name: 'Oklahoma', hint: null },
    { code: 'OR', name: 'Oregon', hint: 'Oregon statewide parcel (ORMAP)' },
    { code: 'RI', name: 'Rhode Island', hint: null },
    { code: 'SC', name: 'South Carolina', hint: null },
    { code: 'SD', name: 'South Dakota', hint: null },
    { code: 'TN', name: 'Tennessee', hint: 'TN county assessors (CoT GIS)' },
    { code: 'TX', name: 'Texas', hint: 'TNRIS / Texas stratmap statewide parcels' },
    { code: 'UT', name: 'Utah', hint: null },
    { code: 'VT', name: 'Vermont', hint: null },
    { code: 'WA', name: 'Washington', hint: 'WA Geospatial Open Data / county assessors' },
    { code: 'WI', name: 'Wisconsin', hint: 'WI statewide parcel initiative (V&E)' },
    { code: 'WY', name: 'Wyoming', hint: null }
  ],
  // Canonical architecture facts so agents don't re-derive them. The source of
  // truth is the code itself — agents are told to read it, not trust this alone.
  briefing: [
    'REPO ROOT: /media/ydn/SYPHER_CORE2/Oracle  (run python as `python3`, NOT `python`).',
    'Harvester spine: backend/harvesters/base.py defines THREE portal archetypes —',
    '  SocrataHarvester  ({base}.json?$limit&$offset),',
    '  ArcGISHarvester   ({service}/query?...&resultOffset),',
    '  CartoHarvester    ({domain}/api/v2/sql?q=SELECT ... LIMIT).',
    'A concrete state subclasses the right archetype, sets its endpoint constant(s),',
    'and implements `map_record(row) -> PropertyRecord | None` against the dataset\'s',
    'REAL column names. PropertyRecord (backend/harvesters/property_adapter.py) fields:',
    '  parcel_id, address, city, state, zip_code, owner_name, owner_type,',
    '  estimated_value(float), equity_percent(float), is_absentee_owner(bool),',
    '  distress_flags(list[str]), last_sale_date(str|None).',
    'Helpers in base.py: to_float(v), classify_owner(name)->"individual"|"corporate"|"trust".',
    'WORKING TEMPLATES to copy the pattern from (read one matching your archetype):',
    '  ArcGIS  -> backend/harvesters/nj_modiv.py , va_vgin.py , nc_onemap.py',
    '  Socrata -> backend/harvesters/ny_pluto.py , ct_sales.py',
    '  CARTO   -> backend/harvesters/pa_philly_opa.py',
    'Prefer a STATEWIDE parcel/assessment dataset that carries owner name + address +',
    'assessed/market value. If only a single large county exists (e.g. CA, AZ, OH, MO),',
    'pick the largest county and set scope to "county:<Name>" — partial is fine, note it.',
    'Do NOT invent endpoints — every URL must be one you actually fetched successfully.'
  ].join('\n')
}

const DISCOVER_SCHEMA = {
  type: 'object',
  properties: {
    code: { type: 'string' },
    found: { type: 'boolean', description: 'true only if you fetched a real dataset successfully' },
    portal_type: { type: 'string', enum: ['arcgis', 'socrata', 'carto', 'none'] },
    endpoint_url: { type: 'string', description: 'the exact base query URL you fetched' },
    dataset_name: { type: 'string' },
    scope: { type: 'string', description: 'statewide | county:<Name> | city:<Name>' },
    column_map: {
      type: 'object',
      description: 'PropertyRecord field -> real source column name',
      properties: {
        parcel_id: { type: 'string' }, address: { type: 'string' },
        city: { type: 'string' }, zip_code: { type: 'string' },
        owner_name: { type: 'string' }, estimated_value: { type: 'string' }
      }
    },
    sample_record: { type: 'string', description: 'one raw JSON row you actually fetched' },
    notes: { type: 'string' }
  },
  required: ['code', 'found', 'portal_type']
}

const GENERATE_SCHEMA = {
  type: 'object',
  properties: {
    code: { type: 'string' },
    written: { type: 'boolean' },
    file_path: { type: 'string', description: 'backend/harvesters/<code>_<slug>.py' },
    class_name: { type: 'string' },
    import_line: { type: 'string', description: 'e.g. from .xx_yy import FooHarvester' },
    registry_entry: { type: 'string', description: 'e.g. "TX": TexasFooHarvester' },
    coverage_entry: { type: 'string', description: 'e.g. "TX": ("Texas ...", "arcgis", "statewide")' },
    notes: { type: 'string' }
  },
  required: ['code', 'written']
}

const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    code: { type: 'string' },
    works: { type: 'boolean', description: 'true only if map_record produced >=1 valid PropertyRecord from live data' },
    record_count: { type: 'integer' },
    sample_parsed: { type: 'string' },
    errors: { type: 'string' },
    verdict: { type: 'string' }
  },
  required: ['code', 'works']
}

const REGISTER_SCHEMA = {
  type: 'object',
  properties: {
    registered_states: { type: 'array', items: { type: 'string' } },
    firehose_updated: { type: 'boolean' },
    coverage_updated: { type: 'boolean' },
    reconcile_test_passed: { type: 'boolean' },
    summary: { type: 'string' }
  },
  required: ['registered_states', 'firehose_updated', 'coverage_updated']
}

function discoverPrompt(st) {
  return [
    `Find a REAL, fetchable open-data property/parcel dataset for ${st.name} (${st.code}).`,
    st.hint ? `Portal hint (public record, verify it): ${st.hint}` : 'No portal hint — research the state GIS clearinghouse / open-data portal and county assessors.',
    '',
    DATA.briefing,
    '',
    'STEPS:',
    '1. Use web search + WebFetch to locate the state (or largest-county) parcel/assessment',
    '   dataset on an ArcGIS FeatureServer, Socrata, or CARTO portal.',
    '2. Actually FETCH a small sample (curl/WebFetch a ?...resultRecordCount=2 or $limit=2 query)',
    '   and read the REAL column names. Owner name + address + assessed value are required;',
    '   skip datasets that lack an owner field.',
    '3. Return the exact base query URL, the platform type, scope, and a column_map from each',
    '   PropertyRecord field to the real source column. Include one raw sample_record.',
    'Set found=false (portal_type "none") if no suitable dataset is reachable — do not guess a URL.'
  ].join('\n')
}

function generatePrompt(disc, st) {
  return [
    `Write the property harvester for ${st.name} (${st.code}).`,
    `Discovery result:\n${JSON.stringify(disc)}`,
    '',
    DATA.briefing,
    '',
    'STEPS:',
    `1. Read backend/harvesters/base.py and the matching template for the "${disc.portal_type}" archetype.`,
    `2. Create backend/harvesters/${st.code.toLowerCase()}_<slug>.py: subclass the correct archetype,`,
    '   set the endpoint constant(s) to the EXACT url from discovery, set STATE/SOURCE_LABEL,',
    '   and implement map_record(row) using the column_map. Use to_float() for value, classify_owner()',
    '   for owner_type, and return None for rows missing a parcel_id or owner. Mark absentee when the',
    '   owner mailing address differs from the situs address if those columns exist.',
    '3. Do NOT edit firehose.py or data_coverage.py — the Register phase does that once. Instead',
    '   RETURN the exact import_line, registry_entry, and coverage_entry strings to use.',
    'If discovery.found is false, set written=false and do nothing.'
  ].join('\n')
}

function verifyPrompt(gen, st) {
  return [
    `Empirically verify the ${st.code} harvester actually parses live data.`,
    `Generate result:\n${JSON.stringify(gen)}`,
    '',
    'Working dir: /media/ydn/SYPHER_CORE2/Oracle . Use python3.',
    'Write a SHORT throwaway script (or python3 -c) that imports the new harvester class,',
    'instantiates it with a dummy tenant_id ("00000000-0000-0000-0000-000000000000"), fetches ONE',
    'small page via the archetype fetch path, and runs map_record on each row. Do NOT call',
    'harvest()/persist_leads — there is no DB. Print how many valid PropertyRecords parsed and one sample.',
    'works=true ONLY if >=1 valid PropertyRecord was produced from a live fetch. Capture any traceback in errors.',
    'If generate.written is false, set works=false with a note.'
  ].join('\n')
}

function registerPrompt(working) {
  const entries = working.map(w => ({
    code: w.code,
    import_line: w.generate.import_line,
    registry_entry: w.generate.registry_entry,
    coverage_entry: w.generate.coverage_entry,
    file_path: w.generate.file_path
  }))
  return [
    'Register every WORKING new harvester into the two shared registries, in ONE pass.',
    `States to register (${entries.length}):\n${JSON.stringify(entries, null, 2)}`,
    '',
    'Working dir: /media/ydn/SYPHER_CORE2/Oracle . Use python3.',
    'STEPS:',
    '1. backend/harvesters/firehose.py — add each import_line near the other harvester imports and',
    '   add each registry_entry to the REGISTRY dict. Keep it valid Python; do not duplicate keys.',
    '2. backend/data_coverage.py — add each coverage_entry to the LIVE_PROPERTY dict, and REMOVE any',
    '   now-covered state from PORTAL_HINTS (a hint for a covered state fails the test).',
    '3. Run the reconciliation check:',
    '   cd /media/ydn/SYPHER_CORE2/Oracle/backend && python3 -c "import data_coverage as dc; from harvesters.firehose import REGISTRY; assert set(dc.LIVE_PROPERTY)==set(REGISTRY), (set(dc.LIVE_PROPERTY)^set(REGISTRY)); print(\'reconcile OK\', len(REGISTRY))"',
    '   and python3 -m backend.data_coverage  to confirm the new totals render.',
    'Set reconcile_test_passed based on whether that assertion passed. Return the list of registered states.'
  ].join('\n')
}

// --------------------------------------------------------------------------- //
// Pipeline: each state flows Discover -> Generate -> Verify independently.
// Stages carry context forward so the final array has the full per-state record.
// --------------------------------------------------------------------------- //
log(`harvest-states: ${DATA.states.length} states through Discover -> Generate -> Verify`)

const results = await pipeline(
  DATA.states,
  // Stage 1 — Discover the real portal + columns.
  (st) =>
    agent(discoverPrompt(st), {
      label: `discover:${st.code}`, phase: 'Discover', model: 'sonnet', schema: DISCOVER_SCHEMA
    }).then(disc => ({ code: st.code, st, discover: disc })),
  // Stage 2 — Generate the harvester file (skip if discovery failed).
  (prev, st) => {
    if (!prev || !prev.discover || !prev.discover.found) {
      return { code: st.code, st, discover: prev?.discover ?? null, generate: { code: st.code, written: false, notes: 'no dataset found' } }
    }
    return agent(generatePrompt(prev.discover, st), {
      label: `generate:${st.code}`, phase: 'Generate', model: 'sonnet', schema: GENERATE_SCHEMA
    }).then(gen => ({ ...prev, generate: gen }))
  },
  // Stage 3 — Verify against live data.
  (prev, st) => {
    if (!prev || !prev.generate || !prev.generate.written) {
      return { ...(prev ?? { code: st.code, st }), verify: { code: st.code, works: false, errors: 'not generated' } }
    }
    return agent(verifyPrompt(prev.generate, st), {
      label: `verify:${st.code}`, phase: 'Verify', model: 'sonnet', schema: VERIFY_SCHEMA
    }).then(v => ({ ...prev, verify: v }))
  }
)

// --------------------------------------------------------------------------- //
// Barrier: register every state that verified, in ONE edit of the shared files.
// (41 agents must NOT edit firehose.py / data_coverage.py concurrently.)
// --------------------------------------------------------------------------- //
const clean = results.filter(Boolean)
const working = clean.filter(r => r.verify && r.verify.works && r.generate && r.generate.written)
log(`Verified working: ${working.length}/${DATA.states.length} — ${working.map(w => w.code).join(', ') || 'none'}`)

let registration = { registered_states: [], firehose_updated: false, coverage_updated: false, summary: 'nothing to register' }
if (working.length > 0) {
  phase('Register')
  registration = await agent(registerPrompt(working), {
    label: 'register-all', phase: 'Register', model: 'sonnet', schema: REGISTER_SCHEMA
  })
}

return {
  attempted: DATA.states.length,
  discovered: clean.filter(r => r.discover && r.discover.found).map(r => r.code),
  generated: clean.filter(r => r.generate && r.generate.written).map(r => r.code),
  verified_working: working.map(r => r.code),
  no_dataset: clean.filter(r => !r.discover || !r.discover.found).map(r => r.code),
  registration,
  per_state: clean.map(r => ({
    code: r.code,
    found: !!(r.discover && r.discover.found),
    written: !!(r.generate && r.generate.written),
    works: !!(r.verify && r.verify.works),
    scope: r.discover && r.discover.scope,
    notes: (r.verify && r.verify.verdict) || (r.generate && r.generate.notes) || (r.discover && r.discover.notes)
  }))
}
