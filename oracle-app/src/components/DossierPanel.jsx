import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { crmGet, crmPut, crmPost } from '../state/useCrmApi';
import { useTour } from '../state/useTour';
import { tourOffer } from '../lib/tour/tourOffer';
import styles from './DossierPanel.module.css';

// The 3D floor-plan editor is a separate chunk (and a separate origin behind
// the iframe) — a dossier opened to read comps should not pay for it.
const RehabEditorDrawer = lazy(() => import('./RehabEditorDrawer'));

// Deal intake was written and never imported, which meant a transaction could
// not be created anywhere in the running product — DealBook could close deals
// and record offers, but nothing could open one. It belongs here because a
// transaction must be anchored to an explicitly chosen property
// (portfolio_api._property_anchor), and the dossier is where one is in hand.
const DealIntakePanel = lazy(() => import('./DealIntakePanel'));

// Stored intelligence for this property. intelligence_api is the largest
// capability here that no screen ever reached — every analysis it produced was
// written and read back by nobody.
const PropertyIntelligencePanel = lazy(() => import('./PropertyIntelligencePanel'));
const IntelligenceAuthoring = lazy(() => import('./IntelligenceAuthoring'));

// Federal/public diligence feeds — FEMA, EPA, FBI, BLS. Eleven routes in
// data_sources_api had no frontend caller at all.
const PublicRecordsDiligence = lazy(() => import('./PublicRecordsDiligence'));

// Photo attach/remove. The GET half was wired, so the product could display a
// filmstrip it had no way to add to and no way to correct.
const PropertyMediaUploader = lazy(() => import('./PropertyMediaUploader'));
// Lazy: PlayCanvas is the heaviest thing the app can load, and most visits to a
// dossier never open a tour.
const TourViewer = lazy(() => import('./TourViewer'));

function money(v) {
  const n = Number(v);
  if (!n || Number.isNaN(n)) return '—';
  return `$${n.toLocaleString()}`;
}

function number(v, suffix = '') {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return 'Not published';
  return `${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
}

function published(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : 'Not published';
}

function reportedDate(value) {
  if (typeof value !== 'string' || !value) return 'Not published';
  const date = new Date(`${value.slice(0, 10)}T12:00:00Z`);
  return Number.isNaN(date.getTime()) ? 'Not published' : date.toLocaleDateString();
}

function coverageScope(value) {
  if (typeof value !== 'string' || !value) return 'Source scope not declared';
  const [kind, ...name] = value.split(':');
  if (!name.length) return kind;
  return `${kind} · ${name.join(':')}`;
}

// Minimal markdown → React for the CMA brief. Handles headings, lists, bold,
// paragraphs. Builds elements directly — no HTML injection surface.
function bold(line, key) {
  const parts = line.split('**');
  return parts.map((p, i) => (i % 2 ? <strong key={`${key}-${i}`}>{p}</strong> : p));
}

function MarkdownBrief({ text }) {
  const blocks = [];
  let list = null;
  text.split('\n').forEach((raw, i) => {
    const line = raw.trim();
    if (line.startsWith('- ') || line.startsWith('* ')) {
      list = list || [];
      list.push(<li key={i}>{bold(line.slice(2), i)}</li>);
      return;
    }
    if (list) {
      blocks.push(<ul key={`ul-${i}`}>{list}</ul>);
      list = null;
    }
    if (!line) return;
    if (line.startsWith('### ')) blocks.push(<h5 key={i}>{bold(line.slice(4), i)}</h5>);
    else if (line.startsWith('## ')) blocks.push(<h4 key={i}>{bold(line.slice(3), i)}</h4>);
    else if (line.startsWith('# ')) blocks.push(<h3 key={i}>{bold(line.slice(2), i)}</h3>);
    else blocks.push(<p key={i}>{bold(line, i)}</p>);
  });
  if (list) blocks.push(<ul key="ul-end">{list}</ul>);
  return <div className={styles.brief}>{blocks}</div>;
}

const INTERACTION_LABEL = {
  voice_note: 'VOICE NOTE',
  portal_view: 'PORTAL VIEW',
  document_signed: 'DOC SIGNED',
  call_transcript: 'CALL',
  sms: 'SMS',
  status_change: 'STATUS',
};

function interactionSummary(entry) {
  const p = entry.payload || {};
  if (entry.interaction_type === 'voice_note') {
    return p.extraction?.action_summary || (p.transcript || '').slice(0, 90) || 'voice note logged';
  }
  return p.summary || p.note || entry.interaction_type.replace(/_/g, ' ');
}

export function DossierPanel({ leadId, onClose }) {
  const [dossier, setDossier] = useState(null);
  const { tour } = useTour({ leadId });
  const [tourOpen, setTourOpen] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState('');
  const [now, setNow] = useState(() => Date.now());

  const [zoning, setZoning] = useState('');
  const [sqft, setSqft] = useState('');
  const [arv, setArv] = useState('');
  const [cma, setCma] = useState('');
  const [cmaBusy, setCmaBusy] = useState(false);
  const [cmaError, setCmaError] = useState('');
  const copyTimer = useRef(null);

  const [editorOpen, setEditorOpen] = useState(false);
  // Bumped when an analysis is authored, so the stored-intelligence list
  // reflects the run that just happened instead of going stale beside it.
  const [intelReloadKey, setIntelReloadKey] = useState(0);
  const [intakeOpen, setIntakeOpen] = useState(false);

  const [entityEditing, setEntityEditing] = useState(false);
  const [entityName, setEntityName] = useState('');
  const [entityEin, setEntityEin] = useState('');
  const [entityState, setEntityState] = useState('');
  const [entityBusy, setEntityBusy] = useState(false);
  const [entityError, setEntityError] = useState('');

  // Reset stale dossier fields when the lead changes — render-phase reset, not
  // inside the effect (the linter flags setState-in-effect).
  const [prevLeadId, setPrevLeadId] = useState(leadId);
  if (leadId !== prevLeadId) { setPrevLeadId(leadId); setDossier(null); setError(''); setCma(''); }

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let live = true;
    crmGet(`/api/leads/${leadId}/dossier`)
      .then((d) => {
        if (!live) return;
        setDossier(d);
        setZoning(d.payload?.zoning_code || '');
        setSqft(String(d.payload?.sqft || d.payload?.square_footage || '') || '');
        setArv(String(d.underwriting?.arv || d.payload?.estimated_value || '') || '');
        setEntityName(d.acquisition_entity?.entity_name || '');
        setEntityEin(d.acquisition_entity?.ein || '');
        setEntityState(d.acquisition_entity?.formation_state || '');
        setEntityEditing(false);
      })
      .catch((err) => live && setError(String(err.message || err)));
    return () => {
      live = false;
      clearTimeout(copyTimer.current);
    };
  }, [leadId]);

  const copy = (key, text) => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(key);
      clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(''), 1600);
    });
  };

  const synthesizeCma = async () => {
    setCmaBusy(true);
    setCmaError('');
    try {
      const d = await crmPost('/api/agents/generate-cma', {
        property_address: dossier.payload?.address || dossier.parcel_id,
        zoning_code: zoning || 'R-1',
        square_footage: Number(sqft) || 0,
        arv_estimate: Number(arv) || 0,
      });
      setCma(d.cma_markdown || '');
    } catch (err) {
      setCmaError(String(err.message || err));
    } finally {
      setCmaBusy(false);
    }
  };

  const saveEntity = async () => {
    setEntityBusy(true);
    setEntityError('');
    try {
      const d = await crmPut(`/api/leads/${leadId}/entity`, {
        entity_name: entityName,
        ein: entityEin,
        formation_state: entityState,
        notes: '',
      });
      setDossier((prev) => ({ ...prev, acquisition_entity: d.acquisition_entity }));
      setEntityEditing(false);
    } catch (err) {
      setEntityError(String(err.message || err));
    } finally {
      setEntityBusy(false);
    }
  };

  // Contract fuse — fraction of the assignment window still unburned.
  let fuse = null;
  if (dossier?.contract_execution_date && dossier?.contract_expires_at) {
    const exec = new Date(dossier.contract_execution_date).getTime();
    const exp = new Date(dossier.contract_expires_at).getTime();
    const frac = Math.min(1, Math.max(0, (exp - now) / (exp - exec)));
    const days = Math.max(0, Math.ceil((exp - now) / 86_400_000));
    fuse = { frac, days, zone: days <= 15 ? 'danger' : days <= 30 ? 'warn' : 'calm' };
  }

  const address = dossier?.payload?.address || dossier?.parcel_id || '…';
  const mkt = dossier?.marketing_payload;
  const property = dossier?.payload || {};
  const provenance = property.provenance || {};
  const quality = property.data_quality || {};
  const hasCoordinates = Number.isFinite(Number(property.latitude))
    && Number.isFinite(Number(property.longitude));

  return (
    <aside className={styles.drawer} role="dialog" aria-modal="false" aria-label={`Asset dossier — ${address}`}>
      <header className={styles.head}>
        <div className={styles.headText}>
          <span className={styles.fileNo}>FILE № {dossier?.parcel_id || leadId.slice(0, 8)}</span>
          <h2 className={styles.address}>{address}</h2>
          <span className={styles.subline}>
            {dossier?.payload?.city ? `${dossier.payload.city} · ` : ''}
            {dossier?.state || ''} · motivation {dossier?.motivation_score ?? '—'}
          </span>
        </div>
        {dossier && (
          <span className={styles.stamp} data-status={dossier.dossier_status}>
            {dossier.dossier_status.replace(/_/g, ' ')}
          </span>
        )}
        <button type="button" className={styles.close} onClick={onClose} aria-label="Close dossier">
          ✕
        </button>
      </header>

      {error && <p className={styles.error}>{error}</p>}
      {!dossier && !error && <p className={styles.loading}>DECRYPTING FILE…</p>}

      {dossier && (
        <div className={styles.body}>
          {/* ── Financial matrix ── */}
          <section className={styles.section} aria-label="Underwriting">
            <h3 className={styles.kicker}>Underwriting Matrix</h3>
            <dl className={styles.matrix}>
              <div><dt>ARV</dt><dd>{money(dossier.underwriting?.arv)}</dd></div>
              <div><dt>MAO</dt><dd>{money(dossier.underwriting?.mao)}</dd></div>
              <div><dt>Rehab</dt><dd>{money(dossier.underwriting?.rehab || dossier.underwriting?.rehab_estimate)}</dd></div>
              <div><dt>Est. Value</dt><dd>{money(dossier.payload?.estimated_value || dossier.underwriting?.estimated_value)}</dd></div>
            </dl>
            <button
              type="button"
              className={styles.floorplanBtn}
              onClick={() => setEditorOpen(true)}
            >
              Edit floor plan &amp; rehab in 3D
            </button>
            <button
              type="button"
              className={styles.floorplanBtn}
              onClick={() => setIntakeOpen((open) => !open)}
              aria-expanded={intakeOpen}
            >
              {intakeOpen ? 'Cancel deal intake' : 'Start a deal from this property'}
            </button>
            {intakeOpen && (
              <Suspense fallback={<p className={styles.loading}>OPENING INTAKE…</p>}>
                <DealIntakePanel
                  propertyId={leadId}
                  propertySource="pipeline"
                  address={address}
                  defaultPrice={
                    dossier.underwriting?.mao
                    || dossier.payload?.estimated_value
                    || dossier.underwriting?.estimated_value
                  }
                  onCreated={() => setIntakeOpen(false)}
                  onCancel={() => setIntakeOpen(false)}
                />
              </Suspense>
            )}
          </section>

          <section className={styles.section} aria-label="Property photos">
            <h3 className={styles.kicker}>Photos</h3>
            <Suspense fallback={null}>
              <PropertyMediaUploader leadId={leadId} />
            </Suspense>
          </section>

          {/* A reconstruction could be produced, stored and resolved, and this
              sheet still showed no way to look at it — the capture existed and
              the viewer existed, with nothing between them. When there is
              nothing to walk, say why: an empty space is indistinguishable
              from a missing feature. */}
          <section className={styles.section} aria-label="3D tour">
            <h3 className={styles.kicker}>3D Tour</h3>
            {tourOffer(tour).kind === 'walkable' ? (
              <>
                <button
                  type="button"
                  className={styles.floorplanBtn}
                  onClick={() => setTourOpen(true)}
                >
                  {tourOffer(tour).label}
                </button>
                {tourOffer(tour).isDemo ? (
                  <p className={styles.emptyNote}>
                    This walkable space is a stand-in, not a capture of this address.
                  </p>
                ) : null}
              </>
            ) : (
              <p className={styles.emptyNote}>{tourOffer(tour).reason}</p>
            )}
          </section>

          {tourOpen && tourOffer(tour).kind === 'walkable' ? (
            <Suspense fallback={null}>
              <TourViewer
                splatUrl={tour.splat_url}
                splatFormat={tour.splat_format}
                splatScene={tour.splat_scene}
                panoScenes={tour.pano_scenes}
                disclosure={tour.disclosure}
                floors={tour.floors}
                address={address}
                title={address}
                // The caveat has to survive into the viewer: once someone is
                // walking around, the button that carried it is off screen.
                isThisProperty={tour.is_this_property !== false}
                onClose={() => setTourOpen(false)}
              />
            </Suspense>
          ) : null}

          <Suspense fallback={null}>
            <PropertyIntelligencePanel
              propertyKey={dossier.parcel_id || ''}
              reloadKey={intelReloadKey}
            />
          </Suspense>

          <Suspense fallback={null}>
            <IntelligenceAuthoring
              propertyKey={dossier.parcel_id || ''}
              onAuthored={() => setIntelReloadKey((n) => n + 1)}
            />
          </Suspense>

          <Suspense fallback={null}>
            <PublicRecordsDiligence
              state={dossier.state || ''}
              zip={dossier.payload?.zip_code || dossier.payload?.zip || ''}
              latitude={property.latitude}
              longitude={property.longitude}
            />
          </Suspense>

          {/* ── Public source detail ── */}
          <section className={styles.section} aria-label="Public property record detail">
            <h3 className={styles.kicker}>Public Property Record</h3>
            <div className={styles.provenance}>
              <div>
                <span>Source</span>
                <strong>{published(provenance.source_name || property.source)}</strong>
              </div>
              <div>
                <span>Coverage</span>
                <strong>{coverageScope(provenance.coverage_scope)}</strong>
              </div>
              <div>
                <span>Detail</span>
                <strong>{published(quality.detail_level).replace(/_/g, ' ')}</strong>
              </div>
            </div>
            <dl className={styles.publicFacts}>
              <div><dt>Owner</dt><dd>{published(property.owner_name)}</dd></div>
              <div><dt>Owner type</dt><dd>{published(property.owner_type)}</dd></div>
              <div><dt>Public value</dt><dd>{money(property.estimated_value)}</dd></div>
              <div><dt>Equity</dt><dd>{number(property.equity_percent, '%')}</dd></div>
              <div><dt>Reported record date</dt><dd>{reportedDate(property.last_sale_date)}</dd></div>
              <div><dt>Absentee flag</dt><dd>{property.is_absentee_owner === true ? 'Reported absentee' : 'Not published'}</dd></div>
              <div><dt>Land use</dt><dd>{published(property.land_use)}</dd></div>
              <div><dt>Zoning</dt><dd>{published(property.zoning_district)}</dd></div>
              <div><dt>Lot area</dt><dd>{number(property.lot_area_sqft, ' sq ft')}</dd></div>
              <div><dt>Building area</dt><dd>{number(property.building_area_sqft, ' sq ft')}</dd></div>
              <div><dt>Max FAR</dt><dd>{number(property.max_far)}</dd></div>
              <div><dt>Map coordinates</dt><dd>{hasCoordinates ? 'Published by source' : 'Not published'}</dd></div>
            </dl>
            <p className={styles.sourceNote}>
              Refreshed {provenance.record_refreshed_at
                ? new Date(provenance.record_refreshed_at).toLocaleString()
                : 'before provenance tracking'} · public records are source-reported, not an ARV,
              title opinion, or outreach authorization.
            </p>
            <p className={styles.emptyNote}>
              Missing facts are intentionally left unfilled. Verify record dates,
              valuation, zoning, and ownership against the authoritative office before use.
            </p>
          </section>

          {/* ── Contract fuse ── */}
          {fuse && (
            <section className={styles.section} aria-label="Assignment window">
              <h3 className={styles.kicker}>Assignment Window</h3>
              <div className={styles.fuseRow}>
                <div className={styles.fuseTrack} role="img" aria-label={`${fuse.days} days remaining`}>
                  <div
                    className={styles.fuseBurn}
                    data-zone={fuse.zone}
                    style={{ width: `${fuse.frac * 100}%` }}
                  />
                </div>
                <span className={styles.fuseDays} data-zone={fuse.zone}>
                  {fuse.days}d
                </span>
              </div>
            </section>
          )}

          {/* ── Entity of record ── */}
          <section className={styles.section} aria-label="Entity of record">
            <h3 className={styles.kicker}>Entity of Record</h3>
            {dossier.acquisition_entity && !entityEditing ? (
              <div className={styles.entityCard}>
                <div className={styles.assetHead}>
                  <span>TITLE VESTS IN</span>
                  <button
                    type="button"
                    className={styles.copyBtn}
                    onClick={() => setEntityEditing(true)}
                  >
                    AMEND
                  </button>
                </div>
                <p className={styles.entityName}>
                  {dossier.acquisition_entity.entity_name}
                </p>
                <p className={styles.entityMeta}>
                  {dossier.acquisition_entity.ein
                    ? `EIN ${dossier.acquisition_entity.ein}`
                    : 'EIN pending'}
                  {dossier.acquisition_entity.formation_state
                    ? ` · ${dossier.acquisition_entity.formation_state}`
                    : ''}
                </p>
              </div>
            ) : (
              <>
                <div className={styles.cmaForm}>
                  <label className={styles.field} style={{ gridColumn: '1 / -1' }}>
                    <span>Entity name</span>
                    <input
                      value={entityName}
                      onChange={(e) => setEntityName(e.target.value)}
                      placeholder="1 Overdue Ln Holdings LLC"
                    />
                  </label>
                  <label className={styles.field}>
                    <span>EIN</span>
                    <input
                      value={entityEin}
                      onChange={(e) => setEntityEin(e.target.value)}
                      placeholder="12-3456789"
                    />
                  </label>
                  <label className={styles.field}>
                    <span>State</span>
                    <input
                      value={entityState}
                      onChange={(e) => setEntityState(e.target.value)}
                      maxLength={2}
                      placeholder="DE"
                    />
                  </label>
                  <button
                    type="button"
                    className={styles.synthesize}
                    disabled={entityBusy || entityName.trim().length < 2}
                    onClick={saveEntity}
                  >
                    {entityBusy ? 'RECORDING…' : 'RECORD ENTITY'}
                  </button>
                </div>
                {entityError && <p className={styles.error}>{entityError}</p>}
                <p className={styles.emptyNote}>
                  Formed by your attorney or filing service — recorded here so
                  the legal package names the correct purchaser.
                </p>
              </>
            )}
          </section>

          {/* ── Disposition assets ── */}
          <section className={styles.section} aria-label="Disposition assets">
            <h3 className={styles.kicker}>Disposition Assets</h3>
            {mkt ? (
              <div className={styles.assets}>
                <div className={styles.assetBlock}>
                  <div className={styles.assetHead}>
                    <span>EMAIL BLAST</span>
                    <button
                      type="button"
                      className={styles.copyBtn}
                      onClick={() => copy('email', `${mkt.email_subject}\n\n${mkt.email_body}`)}
                    >
                      {copied === 'email' ? 'COPIED' : 'COPY'}
                    </button>
                  </div>
                  <p className={styles.assetSubject}>{mkt.email_subject}</p>
                  <p className={styles.assetText}>{mkt.email_body}</p>
                </div>
                <div className={styles.assetBlock}>
                  <div className={styles.assetHead}>
                    <span>IG CAPTION</span>
                    <button
                      type="button"
                      className={styles.copyBtn}
                      onClick={() => copy('ig', mkt.ig_caption)}
                    >
                      {copied === 'ig' ? 'COPIED' : 'COPY'}
                    </button>
                  </div>
                  <p className={styles.assetText}>{mkt.ig_caption}</p>
                </div>
              </div>
            ) : (
              <p className={styles.emptyNote}>
                None generated — assets synthesize automatically when the window
                enters the danger zone.
              </p>
            )}
          </section>

          {/* ── CMA synthesizer ── */}
          <section className={styles.section} aria-label="CMA synthesizer">
            <h3 className={styles.kicker}>CMA Synthesizer</h3>
            <div className={styles.cmaForm}>
              <label className={styles.field}>
                <span>Zoning</span>
                <input value={zoning} onChange={(e) => setZoning(e.target.value)} placeholder="R-2" />
              </label>
              <label className={styles.field}>
                <span>Sqft</span>
                <input value={sqft} onChange={(e) => setSqft(e.target.value)} inputMode="numeric" placeholder="1800" />
              </label>
              <label className={styles.field}>
                <span>ARV $</span>
                <input value={arv} onChange={(e) => setArv(e.target.value)} inputMode="numeric" placeholder="250000" />
              </label>
              <button
                type="button"
                className={styles.synthesize}
                disabled={cmaBusy || !Number(sqft) || !Number(arv)}
                onClick={synthesizeCma}
              >
                {cmaBusy ? 'SYNTHESIZING…' : 'SYNTHESIZE BRIEF'}
              </button>
            </div>
            {cmaError && <p className={styles.error}>{cmaError}</p>}
            {cma && (
              <div className={styles.briefWrap}>
                <div className={styles.assetHead}>
                  <span>LISTING BRIEF</span>
                  <button type="button" className={styles.copyBtn} onClick={() => copy('cma', cma)}>
                    {copied === 'cma' ? 'COPIED' : 'COPY MD'}
                  </button>
                </div>
                <MarkdownBrief text={cma} />
              </div>
            )}
          </section>

          {/* ── Interaction stream ── */}
          {dossier.interactions.length > 0 && (
            <section className={styles.section} aria-label="Interaction history">
              <h3 className={styles.kicker}>Interaction Stream</h3>
              <ul className={styles.stream}>
                {dossier.interactions.map((entry, i) => (
                  <li key={i} className={styles.streamLine}>
                    <span className={styles.streamType} data-role={entry.actor_role}>
                      {INTERACTION_LABEL[entry.interaction_type] || entry.interaction_type}
                    </span>
                    <span className={styles.streamText}>{interactionSummary(entry)}</span>
                    <time className={styles.streamTime}>
                      {new Date(entry.created_at).toLocaleDateString()}
                    </time>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}

      {editorOpen && (
        <Suspense fallback={null}>
          <RehabEditorDrawer
            leadId={leadId}
            onClose={() => setEditorOpen(false)}
            onSaved={() => {
              // Re-pull the dossier so the Underwriting matrix reflects the
              // rehab total the server just recomputed from the new layout.
              crmGet(`/api/leads/${leadId}/dossier`).then(setDossier, () => {});
            }}
          />
        </Suspense>
      )}
    </aside>
  );
}
