import { useEffect, useRef } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Compass,
  X,
} from 'lucide-react';
import { placementFor, useSpotlight } from './useSpotlight';
import styles from './ProductTour.module.css';

const STEPS = [
  { kind: 'tab', destination: 'today', anchor: '#tab-today', label: 'Today', title: 'Start with the next best work', detail: 'Review appointments, overdue tasks, active deals, and evidence-backed priorities before opening individual records.', verify: 'Counts and recommendations should reflect live CRM records, never sample data.' },
  { kind: 'tab', destination: 'people', anchor: '#tab-people', label: 'People', title: 'One relationship record', detail: 'Search contacts, manage buyer and seller opportunities, open the full activity history, and keep assignment ownership explicit.', verify: 'Try an exact email, phone, or name-prefix search and open a contact drawer.' },
  { kind: 'tab', destination: 'inbox', anchor: '#tab-inbox', label: 'Inbox', title: 'Every conversation in context', detail: 'Work email, SMS, and call history alongside the contact and opportunity that produced it.', verify: 'Outbound drafts remain reviewable and channels stay unavailable until their provider validates.' },
  { kind: 'tab', destination: 'deals', anchor: '#tab-deals', label: 'Deals', title: 'Search to settlement', detail: 'Track offer stages, deadlines, documents, state checklists, transaction milestones, and compliance holdings in one workflow.', verify: 'Open a deal and confirm deadlines and documents remain tied to the correct tenant and transaction.' },
  { kind: 'workspace', destination: 'cowork', anchor: '#tab-studio', label: 'Our AI · Cowork', title: 'Command the operating system', detail: 'Ask NEOH to research, summarize, compare, and stage work across the CRM while retaining source and approval boundaries.', verify: 'Ask for a revenue brief and inspect the cited records before accepting any recommendation.' },
  { kind: 'workspace', destination: 'sales', anchor: '#tab-studio', label: 'Our AI · Sales', title: 'Turn CRM truth into action', detail: 'Prioritize leads, qualify intent, draft personalized follow-up, and hand the full context back to the human agent.', verify: 'Capability badges come from live backend and provider health—not marketing copy.' },
  { kind: 'sales', destination: '/our-ai/sales/agent', anchor: '[data-tour-anchor="/our-ai/sales/agent"]', label: 'Sales Agent', title: 'Qualify and hand off', detail: 'Summarize a relationship, identify missing qualification facts, create a task, or prepare an editable email or SMS draft.', verify: 'Staging a draft must never send it; outbound delivery still requires approval.' },
  { kind: 'sales', destination: '/our-ai/sales/routing', anchor: '[data-tour-anchor="/our-ai/sales/routing"]', label: 'Lead Routing', title: 'Capture and assign instantly', detail: 'Create signed lead connectors, apply source, ZIP, state, and intent rules, then route only to active agents with capacity.', verify: 'Webhook secrets appear once, payloads stay encrypted, and duplicate event IDs never create duplicate contacts.' },
  { kind: 'sales', destination: '/our-ai/sales/dialer', anchor: '[data-tour-anchor="/our-ai/sales/dialer"]', label: 'Power Dialer', title: 'Call from verified identity', detail: 'Build a CRM-grounded call list, review scripts, place browser calls, and preserve call records and compliance decisions.', verify: 'Calling remains disabled until a provider, route, and verified caller ID all pass validation.' },
  { kind: 'sales', destination: '/our-ai/sales/plans', anchor: '[data-tour-anchor="/our-ai/sales/plans"]', label: 'Smart Plans', title: 'Build repeatable nurture', detail: 'Version multi-step wait, task, email, SMS, and approved-call workflows; preview contact impact before enrollment.', verify: 'Publish a revision before enrollment and confirm every outbound step lands in the approval queue.' },
  { kind: 'sales', destination: '/our-ai/sales/providers', anchor: '[data-tour-anchor="/our-ai/sales/providers"]', label: 'Providers', title: 'Make delivery truth visible', detail: 'Connect Google, Twilio, Azure Communication Services, or Amazon SES with encrypted, tenant-scoped credentials.', verify: 'Saving a credential does not mark it connected; use Validate and inspect each enabled capability.' },
  { kind: 'sales', destination: '/our-ai/sales/providers', anchor: '[data-tour-anchor="/our-ai/sales/providers"]', label: 'Live hand-off', title: 'Give NEOH your phone number', detail: 'On the telephony route, set "Your phone for live hand-off". When a caller asks for a person — or the assistant is unavailable — NEOH says it is connecting them and bridges the live call straight to that number.', verify: 'Leave the number blank and hand-off stays off; the caller is told to expect a callback instead of being dropped.' },
  { kind: 'workspace', destination: 'social', anchor: '#tab-studio', label: 'Our AI · Social', title: 'Plan local content before publishing', detail: 'Create market-aware calendars, listing campaigns, and channel-specific drafts using verified property and market facts.', verify: 'Review drafts and sources. Publishing and paid media remain off until the relevant account is linked.' },
  { kind: 'workspace', destination: 'homeowners', anchor: '#tab-studio', label: 'Our AI · Homeowners', title: 'Develop the seller database', detail: 'Find homeowner intent, prepare property and equity reviews, monitor nurture gaps, and pause automation at human handoff.', verify: 'Intent must show evidence and uncertainty; protected traits are never used for scoring.' },
  { kind: 'workspace', destination: 'automations', anchor: '#tab-studio', label: 'Our AI · Automations', title: 'Delegate governed repetitive work', detail: 'Describe recurring jobs, choose autonomy limits, monitor runs, and keep consequential actions inside review gates.', verify: 'Inspect the action level before enabling an automation and use the activity log to trace every result.' },
  { kind: 'workspace', destination: 'sites', anchor: '#tab-studio', label: 'Our AI · Sites', title: 'Own the local audience', detail: 'Build responsive hyperlocal sites, attach only authorized IDX sources, preview revisions, and publish with attribution intact.', verify: 'Review desktop and mobile previews and confirm MLS authorization before enabling IDX.' },
];

// Every navigation here REPLACES the history entry. The tour has its own
// Previous/Next controls, so mirroring each step into browser history would
// only bury whatever page the user opened the tour from.
function visit(step, onNavigateTab, onNavigateSales) {
  if (step.kind === 'tab') {
    onNavigateTab(step.destination, true);
    return undefined;
  }
  if (step.kind === 'sales') {
    onNavigateSales(step.destination, true);
    return undefined;
  }
  window.sessionStorage.setItem('oracle_ai_workspace', step.destination);
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
      if (event.key === 'Escape') onClose(false);
      if (event.key === 'ArrowRight' && event.altKey && stepIndex < STEPS.length - 1) onStepChange(stepIndex + 1);
      if (event.key === 'ArrowLeft' && event.altKey && stepIndex > 0) onStepChange(stepIndex - 1);
    };
    window.addEventListener('keydown', onKeyDown);
    const focusFrame = window.requestAnimationFrame(() => panelRef.current?.focus({ preventScroll: true }));
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.cancelAnimationFrame(focusFrame);
    };
  }, [onClose, onStepChange, open, stepIndex]);

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
          highlighted control. */}
      <div className={styles.spotlightLayer} aria-hidden="true" data-resolved={spotlight ? 'true' : 'false'}>
        {spotlight ? (
          <>
            <div className={styles.scrim} style={{ top: 0, left: 0, right: 0, height: `${Math.max(0, spotlight.top)}px` }} />
            <div className={styles.scrim} style={{ top: `${spotlight.top + spotlight.height}px`, left: 0, right: 0, bottom: 0 }} />
            <div className={styles.scrim} style={{ top: `${spotlight.top}px`, left: 0, width: `${Math.max(0, spotlight.left)}px`, height: `${spotlight.height}px` }} />
            <div className={styles.scrim} style={{ top: `${spotlight.top}px`, left: `${spotlight.left + spotlight.width}px`, right: 0, height: `${spotlight.height}px` }} />
            <div
              className={styles.halo}
              style={{
                top: `${spotlight.top}px`,
                left: `${spotlight.left}px`,
                width: `${spotlight.width}px`,
                height: `${spotlight.height}px`,
              }}
            />
          </>
        ) : (
          // Target not found (or not laid out yet): no cut-out, no dimming, so
          // we never darken the screen while pointing at nothing.
          null
        )}
      </div>

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
        <button type="button" onClick={() => onClose(false)} aria-label="Close walkthrough"><X aria-hidden="true" /></button>
      </header>
      <div className={styles.progress} role="progressbar" aria-label="Walkthrough progress" aria-valuemin="1" aria-valuemax={STEPS.length} aria-valuenow={stepIndex + 1}><span style={{ width: `${progress}%` }} /></div>
      <div className={styles.body}>
        <div className={styles.eyebrow}>Step {stepIndex + 1} of {STEPS.length} · {step.label}</div>
        <h2 id="product-tour-title">{step.title}</h2>
        <p>{step.detail}</p>
        <div className={styles.verify}><Check aria-hidden="true" /><span><strong>Try it</strong>{step.verify}</span></div>
      </div>
      <footer className={styles.footer}>
        <button type="button" className={styles.previous} onClick={() => onStepChange(stepIndex - 1)} disabled={stepIndex === 0}><ArrowLeft aria-hidden="true" /> Previous</button>
        <button type="button" className={styles.next} onClick={() => last ? onClose(true) : onStepChange(stepIndex + 1)}>{last ? 'Finish tour' : 'Next'} {!last ? <ArrowRight aria-hidden="true" /> : <Check aria-hidden="true" />}</button>
      </footer>
    </aside>
    </>
  );
}
