import { useEffect, useRef } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronLeft,
  ChevronRight,
  Compass,
  X,
} from 'lucide-react';
import { placementFor, useSpotlight } from './useSpotlight';
import styles from './ProductTour.module.css';

// Current workflow is 3 views (Home / Work / Neoh). Work holds everything
// searchable plus Our AI workspaces; Neoh is the conversation. Steps mirror
// that shape and use live anchors so the spotlight stays on what exists.
// Legacy ids (today, inbox) are kept as aliases in routes.js but steps now
// name the current view/type so a reader can trace them without the alias
// table. Sales sub-steps keep their data-tour-anchor hooks in SalesWorkspace.
const STEPS = [
  // ── Home ───────────────────────────────────────────────────────────────
  { kind: 'tab', destination: 'home', anchor: '#tab-home', label: 'Home', title: 'Start with what matters', detail: 'Home says what needs you right now — the ranked opportunities Neoh surfaced, what it handled for you, and why. One sentence at the top; at most three asks.', verify: 'If Home says nothing, it says why: low confidence held back, or a feed is unavailable — never a silent empty state.' },
  // ── Work — the unified workspace ──────────────────────────────────────
  { kind: 'tab', destination: 'work', anchor: '#tab-work', label: 'Work', title: 'Everything, searchable', detail: 'Work is one searchable workspace. Type a name, address, or deal term; four chips filter the search (People, Properties, Deals, Conversations). Empty shows Recent — what you actually touched.', verify: 'Type two characters and see grouped, live results. A failed search leg is named, never hidden.' },
  { kind: 'work', destination: 'recent', anchor: '[data-tour-anchor="work-search"]', label: 'Work · Recent', title: 'Pick up where you left off', detail: 'Recent is the trail — the last 8 people, properties, deals, or conversations you opened. No KPI tiles; it is navigation, not a report.', verify: 'Open a recent hit and confirm it becomes a sheet over Work, and Back closes the sheet.' },
  { kind: 'work', destination: 'people', anchor: '[data-tour-anchor="work-chip-people"]', label: 'People', title: 'One relationship record', detail: 'Search contacts, manage buyer and seller opportunities, open the full activity history, and keep assignment ownership explicit.', verify: 'Try an exact email, phone, or name-prefix search and open a contact drawer.' },
  { kind: 'work', destination: 'conversations', anchor: '[data-tour-anchor="work-chip-conversations"]', label: 'Conversations', title: 'Every conversation in context', detail: 'Work email, SMS, and call history alongside the contact and opportunity that produced it. Views and sequencing stay tenant-scoped.', verify: 'Outbound drafts remain reviewable and channels stay unavailable until their provider validates.' },
  { kind: 'work', destination: 'deals', anchor: '[data-tour-anchor="work-chip-deals"]', label: 'Deals', title: 'Search to settlement', detail: 'Track offer stages, deadlines, documents, state checklists, transaction milestones, and compliance holdings in one pipeline.', verify: 'Open a deal and confirm deadlines and documents remain tied to the correct tenant and transaction.' },
  { kind: 'work', destination: 'properties', anchor: '[data-tour-anchor="work-chip-properties"]', label: 'Properties', title: 'Records, listings, tours', detail: 'Properties, MLS listings, market panels, and 3D tours live together. A property sheet can expand into its immersive tour and fold back.', verify: 'Open a property sheet and try the 3D tour; a capture renders instead of a black canvas.' },
  { kind: 'work', destination: 'opportunities', anchor: '#tab-work', label: 'Opportunities', title: 'Ranked asks, with evidence', detail: 'The Intelligence feed behind Work ranks what deserves attention, and every card shows its why, confidence, and evidence — not raw column names.', verify: 'Expand Why? and check evidence freshness (as of) rather than source table paths.' },
  { kind: 'work', destination: 'ai', anchor: '#ai-workspace-tab-command', label: 'Our AI · Command', title: 'Command the operating system', detail: 'Our AI lives inside Work (type=ai). Command and Intelligence are live dashboards; Cowork, Sales, Social, Homeowners, Automations, and Sites are the workspaces beneath.', verify: 'Ask for a revenue brief and inspect the cited records before accepting any recommendation. Capability badges are live, not marketing copy.' },
  { kind: 'workspace', destination: 'cowork', anchor: '#ai-workspace-tab-cowork', label: 'Cowork', title: 'Work across the business', detail: 'Ask NEOH to research, summarize, compare, and stage work across the CRM while retaining source and approval boundaries.', verify: 'Try "Research this property" and check that sources are cited and missing facts are named.' },
  { kind: 'workspace', destination: 'sales', anchor: '#ai-workspace-tab-sales', label: 'Our AI · Sales', title: 'Turn CRM truth into action', detail: 'Sales routes CRM truth into prioritize → qualify → follow-up, and hands the full context back to the human. Five destinations live under Sales.', verify: 'Open Sales and confirm the four Sales metrics (contacts, threads, routes, calls) are live counts.' },
  { kind: 'sales', destination: '/our-ai/sales/agent', anchor: '[data-tour-anchor="/our-ai/sales/agent"]', label: 'Sales Agent', title: 'Qualify and hand off', detail: 'Summarize a relationship, identify missing qualification facts, create a task, or prepare an editable email or SMS draft.', verify: 'Staging a draft must never send it; outbound delivery still requires approval.' },
  { kind: 'sales', destination: '/our-ai/sales/routing', anchor: '[data-tour-anchor="/our-ai/sales/routing"]', label: 'Lead Routing', title: 'Capture and assign instantly', detail: 'Create signed lead connectors, apply source, ZIP, state, and intent rules, then route only to active agents with capacity.', verify: 'Webhook secrets appear once, payloads stay encrypted, and duplicate event IDs never create duplicate contacts.' },
  { kind: 'sales', destination: '/our-ai/sales/dialer', anchor: '[data-tour-anchor="/our-ai/sales/dialer"]', label: 'Power Dialer', title: 'Call from verified identity', detail: 'Build a CRM-grounded call list, review scripts, place browser calls, and preserve call records and compliance decisions.', verify: 'Calling remains disabled until a provider, route, and verified caller ID all pass validation.' },
  { kind: 'sales', destination: '/our-ai/sales/plans', anchor: '[data-tour-anchor="/our-ai/sales/plans"]', label: 'Smart Plans', title: 'Build repeatable nurture', detail: 'Version multi-step wait, task, email, SMS, and approved-call workflows; preview contact impact before enrollment.', verify: 'Publish a revision before enrollment and confirm every outbound step lands in the approval queue.' },
  { kind: 'sales', destination: '/our-ai/sales/providers', anchor: '[data-tour-anchor="/our-ai/sales/providers"]', label: 'Providers', title: 'Make delivery truth visible', detail: 'Connect Google, Twilio, or your own SMTP server with encrypted, tenant-scoped credentials.', verify: 'Saving a credential does not mark it connected; use Validate and inspect each enabled capability.' },
  { kind: 'workspace', destination: 'social', anchor: '#ai-workspace-tab-social', label: 'Social', title: 'Plan local content before publishing', detail: 'Create market-aware calendars, listing campaigns, and channel-specific drafts using verified property and market facts.', verify: 'Review drafts and sources. Publishing and paid media remain off until the relevant account is linked.' },
  { kind: 'workspace', destination: 'homeowners', anchor: '#ai-workspace-tab-homeowners', label: 'Homeowners', title: 'Develop the seller database', detail: 'Find homeowner intent, prepare property and equity reviews, monitor nurture gaps, and pause automation at human handoff.', verify: 'Intent must show evidence and uncertainty; protected traits are never used for scoring.' },
  { kind: 'workspace', destination: 'automations', anchor: '#ai-workspace-tab-automations', label: 'Automations', title: 'Delegate governed repetitive work', detail: 'Describe recurring jobs, choose autonomy limits, monitor runs, and keep consequential actions inside review gates.', verify: 'Inspect the action level before enabling an automation and use the activity log to trace every result.' },
  { kind: 'workspace', destination: 'sites', anchor: '#ai-workspace-tab-sites', label: 'Sites', title: 'Own the local audience', detail: 'Build responsive hyperlocal sites, attach only authorized IDX sources, preview revisions, and publish with attribution intact.', verify: 'Review desktop and mobile previews and confirm MLS authorization before enabling IDX.' },
  { kind: 'tab', destination: 'neoh', anchor: '#tab-neoh', label: 'Neoh', title: 'Ask anything, stay in the record', detail: 'Neoh is the conversation over every view. Ask, attach a PDF or photo, and review staged changes — reversible, cited, tenant-safe. One surface, docked above the deck.', verify: 'Attach a file and confirm Neoh reads it; try Undo on a safe internal-field update.' },
];

// Bare ArrowLeft/ArrowRight drive the tour, but the same keys move a caret,
// scrub a <select>, or roll focus through a role="tab" list — all of which
// this step's spotlighted control may be. Only steal the arrow when focus is
// nowhere that already claims it.
function arrowKeysAreFree() {
  const el = document.activeElement;
  if (!el) return true;
  const tag = el.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return false;
  if (el.isContentEditable) return false;
  if (el.closest('[role="tablist"], [role="listbox"], [role="slider"]')) return false;
  return true;
}

// Every navigation here REPLACES the history entry. The tour has its own
// Previous/Next controls, so mirroring each step into browser history would
// only bury whatever page the user opened the tour from.
// Now also handles 'work' (a Work?type) so tour tracks live workflow types.
function visit(step, onNavigateTab, onNavigateSales) {
  if (step.kind === 'tab') {
    onNavigateTab(step.destination, true);
    return undefined;
  }
  if (step.kind === 'work') {
    // A Work type is a view; use the tab handler with the type as id.
    // resolveLegacyId in routes.js maps it to Work view correctly.
    onNavigateTab(step.destination, true);
    return undefined;
  }
  if (step.kind === 'sales') {
    onNavigateSales(step.destination, true);
    return undefined;
  }
  window.sessionStorage.setItem('oracle_ai_workspace', step.destination);
  // For Our AI workspaces inside Work?type=ai, go to Work first then
  // dispatch the workspace id. The timeout lets the Work view mount before
  // OurAITab listens for the event.
  onNavigateSales(step.destination === 'sales' ? '/our-ai/sales' : '/our-ai', true);
  const timer = window.setTimeout(() => {
    window.dispatchEvent(new CustomEvent('oracle:ai-workspace', { detail: step.destination }));
  }, 80);
  return () => window.clearTimeout(timer);
}

export function ProductTour({ open, stepIndex, onStepChange, onClose, onNavigateTab, onNavigateSales }) {
  const panelRef = useRef(null);
  const step = STEPS[Math.min(Math.max(stepIndex, 0), STEPS.length - 1)];
  // Tracks the current step's target through navigation, lazy mounts, and
  // scrolling. Null until it resolves, which the render treats as "no cut-out".
  const spotlight = useSpotlight(step.anchor, { active: open });

  useEffect(() => {
    if (!open) return undefined;
    return visit(step, onNavigateTab, onNavigateSales);
  }, [onNavigateSales, onNavigateTab, open, step]);

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose(false);
        return;
      }
      // Bare ArrowLeft/ArrowRight advance the tour, but not while focus is on
      // a control that already uses them (a text field, a select, a tab
      // list) — see arrowKeysAreFree().
      if (event.key === 'ArrowRight' && stepIndex < STEPS.length - 1 && arrowKeysAreFree()) {
        event.preventDefault();
        onStepChange(stepIndex + 1);
      }
      if (event.key === 'ArrowLeft' && stepIndex > 0 && arrowKeysAreFree()) {
        event.preventDefault();
        onStepChange(stepIndex - 1);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    const focusFrame = window.requestAnimationFrame(() => panelRef.current?.focus({ preventScroll: true }));
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.cancelAnimationFrame(focusFrame);
    };
  }, [onClose, onStepChange, open, stepIndex]);

  // Live workflow: if the user navigates on their own while the walkthrough
  // is open (e.g., clicks a chip), keep the scrim tracking. No-op if the
  // navigation was tour-driven — visit already placed them correctly.

  if (!open) return null;
  const last = stepIndex === STEPS.length - 1;
  const progress = ((stepIndex + 1) / STEPS.length) * 100;
  const placement = placementFor(spotlight);
  const anchoredStyle = placement.position === 'anchored'
    ? { top: `${placement.top}px`, left: `${placement.left}px`, right: 'auto', bottom: 'auto' }
    : undefined;

  return (
    <>
      {/* Dim layer with a cut-out over the step's target. Four panels rather
          than an SVG mask so the hole stays clickable — the walkthrough is
          non-modal by design, and the user is meant to actually press the
          highlighted control. Clicking the dimmed scrim exits the tour; clicking
          the hole interacts with the underlying control. */}
      <div className={styles.spotlightLayer} aria-hidden="true" data-resolved={spotlight ? 'true' : 'false'}>
        {spotlight ? (
          <>
            <button type="button" tabIndex={-1} aria-label="Exit guided walkthrough" className={styles.scrim} style={{ top: 0, left: 0, right: 0, height: `${Math.max(0, spotlight.top)}px` }} onClick={() => onClose(false)} />
            <button type="button" tabIndex={-1} aria-label="Exit guided walkthrough" className={styles.scrim} style={{ top: `${spotlight.top + spotlight.height}px`, left: 0, right: 0, bottom: 0 }} onClick={() => onClose(false)} />
            <button type="button" tabIndex={-1} aria-label="Exit guided walkthrough" className={styles.scrim} style={{ top: `${spotlight.top}px`, left: 0, width: `${Math.max(0, spotlight.left)}px`, height: `${spotlight.height}px` }} onClick={() => onClose(false)} />
            <button type="button" tabIndex={-1} aria-label="Exit guided walkthrough" className={styles.scrim} style={{ top: `${spotlight.top}px`, left: `${spotlight.left + spotlight.width}px`, right: 0, height: `${spotlight.height}px` }} onClick={() => onClose(false)} />
            <div
              className={styles.halo}
              aria-hidden="true"
              style={{
                top: `${spotlight.top}px`,
                left: `${spotlight.left}px`,
                width: `${spotlight.width}px`,
                height: `${spotlight.height}px`,
              }}
            />
          </>
        ) : (
          // Target not found (or not laid out yet): no cut-out, no dimming,
          // and — unlike the resolved scrim above — no click-to-exit either.
          // A lazy-mounted anchor (Our AI workspaces, sub-tabs) can still be
          // resolving when the user tries the exact click the step told them
          // to make; a full-screen scrim there swallowed that click as "exit
          // the tour" instead. The panel's own Exit/X stays reachable.
          null
        )}
      </div>

      {/* Floating forward/back that travel with the walkthrough — always
          tappable regardless of where the panel anchors, mirroring the
          keyboard ArrowLeft/ArrowRight and the footer buttons. */}
      <nav className={styles.stepper} aria-label="Walkthrough navigation">
        <button
          type="button"
          className={styles.stepBtn}
          onClick={() => onStepChange(stepIndex - 1)}
          disabled={stepIndex === 0}
          aria-label="Previous step"
        >
          <ChevronLeft aria-hidden="true" />
        </button>
        <button
          type="button"
          className={styles.stepBtn}
          onClick={() => (last ? onClose(true) : onStepChange(stepIndex + 1))}
          aria-label={last ? 'Finish tour' : 'Next step'}
        >
          {last ? <Check aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
        </button>
      </nav>

    <aside
      id="crm-product-tour"
      ref={panelRef}
      className={styles.tour}
      data-placement={placement.position}
      style={anchoredStyle}
      role="dialog"
      aria-modal="false"
      aria-labelledby="product-tour-title"
      tabIndex={-1}
    >
      <header className={styles.header}>
        <span><Compass aria-hidden="true" /> Guided walkthrough</span>
        <div className={styles.headerActions}>
          <button type="button" className={styles.skip} onClick={() => onClose(false)} aria-label="Exit guided walkthrough">Exit</button>
          <button type="button" onClick={() => onClose(false)} aria-label="Close walkthrough"><X aria-hidden="true" /></button>
        </div>
      </header>
      <div className={styles.progress} role="progressbar" aria-label="Walkthrough progress" aria-valuemin="1" aria-valuemax={STEPS.length} aria-valuenow={stepIndex + 1}><span style={{ width: `${progress}%` }} /></div>
      <div className={styles.body}>
        <div className={styles.eyebrow}>Step {stepIndex + 1} of {STEPS.length} · {step.label}</div>
        <h2 id="product-tour-title">{step.title}</h2>
        <p>{step.detail}</p>
        <div className={styles.verify}><Check aria-hidden="true" /><span><strong>Try it</strong>{step.verify}</span></div>
      </div>
      <footer className={styles.footer}>
        <div className={styles.footerLeft}>
          <button type="button" className={styles.previous} onClick={() => onStepChange(stepIndex - 1)} disabled={stepIndex === 0}><ArrowLeft aria-hidden="true" /> Previous</button>
          <button type="button" className={styles.exit} onClick={() => onClose(false)}>Exit tour</button>
        </div>
        <button type="button" className={styles.next} onClick={() => last ? onClose(true) : onStepChange(stepIndex + 1)}>{last ? 'Finish tour' : 'Next'} {!last ? <ArrowRight aria-hidden="true" /> : <Check aria-hidden="true" />}</button>
      </footer>
    </aside>
    </>
  );
}
