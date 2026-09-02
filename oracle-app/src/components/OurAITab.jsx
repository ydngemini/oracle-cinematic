import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  ArrowUpRight,
  Bot,
  BriefcaseBusiness,
  CalendarDays,
  CheckCircle2,
  CircleDashed,
  FileCheck2,
  Globe2,
  House,
  Mail,
  MapPin,
  Megaphone,
  MessageSquare,
  PhoneCall,
  PlugZap,
  RefreshCw,
  Radar,
  Route,
  Search,
  Share2,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Users,
  Workflow,
} from 'lucide-react';
import { crmGet } from '../state/useCrmApi';
import { useAssistant } from './AssistantContext';
import styles from './OurAITab.module.css';

const PersonalAITab = lazy(() => import('./PersonalAITab'));
const IntelligenceFeed = lazy(() =>
  import('./IntelligenceFeed').then((m) => ({ default: m.IntelligenceFeed })));
const SalesWorkspace = lazy(() => import('./SalesWorkspace'));
const StudioTab = lazy(() => import('./StudioTab'));

const WORKSPACE_KEY = 'oracle_ai_workspace';

const WORKSPACES = [
  // First on purpose. The promise is that Neoh says what needs attention
  // before the agent thinks to ask; opening on a prompt would contradict it.
  { id: 'intelligence', label: 'Intelligence', Icon: Radar },
  { id: 'cowork', label: 'Cowork', Icon: Bot },
  { id: 'sales', label: 'Sales', Icon: PhoneCall },
  { id: 'social', label: 'Social', Icon: Share2 },
  { id: 'homeowners', label: 'Homeowners', Icon: House },
  { id: 'automations', label: 'Automations', Icon: Workflow },
  { id: 'sites', label: 'Sites', Icon: Globe2 },
];

const SOURCES = [
  { id: 'assistant', label: 'NEOH assistant', path: '/api/ai/chat/status', select: (payload) => payload || {} },
  { id: 'commands', label: 'Approval queue', path: '/api/commands?limit=20', select: (payload) => payload?.commands || [] },
  { id: 'providers', label: 'Provider links', path: '/api/commands/providers', select: (payload) => payload?.providers || [] },
  { id: 'contacts', label: 'Contacts', path: '/api/crm/contacts?limit=200', select: (payload) => payload?.contacts || [] },
  { id: 'clients', label: 'Opportunities', path: '/api/crm/clients?type=all&sort=recent', select: (payload) => payload?.clients || [] },
  { id: 'segments', label: 'Saved audiences', path: '/api/crm/clients/segments', select: (payload) => payload?.segments || [] },
  { id: 'threads', label: 'Conversations', path: '/api/crm/comms/threads', select: (payload) => payload?.threads || [] },
  { id: 'transactions', label: 'Transactions', path: '/api/portfolio/transactions?limit=100', select: (payload) => payload?.transactions || [] },
  { id: 'routes', label: 'Voice routes', path: '/api/telephony/routes', select: (payload) => payload?.routes || [] },
  { id: 'calls', label: 'Inbound calls', path: '/api/telephony/calls?limit=50', select: (payload) => payload?.calls || [] },
  { id: 'sites', label: 'Websites', path: '/api/sites', select: (payload) => payload?.sites || payload?.items || [] },
  { id: 'idx', label: 'MLS / IDX', path: '/api/mls/health', select: (payload) => payload || {} },
  { id: 'sales', label: 'Sales stack', path: '/api/sales/capabilities', select: (payload) => payload || {} },
  { id: 'routing', label: 'Lead routing', path: '/api/crm/routing/metrics?days=30', select: (payload) => payload || {} },
];

const STATUS = {
  live: { label: 'Live', tone: 'good' },
  ready: { label: 'Draft ready', tone: 'good' },
  partial: { label: 'Partial', tone: 'warn' },
  setup: { label: 'Setup required', tone: 'neutral' },
  offline: { label: 'Unavailable', tone: 'danger' },
  checking: { label: 'Checking', tone: 'neutral' },
  disabled: { label: 'Disabled', tone: 'neutral' },
};

function savedWorkspace() {
  if (typeof window === 'undefined') return 'cowork';
  const stored = window.sessionStorage.getItem(WORKSPACE_KEY);
  return WORKSPACES.some((workspace) => workspace.id === stored) ? stored : 'cowork';
}

function hasOwn(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function formatNumber(value) {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(Number(value) || 0);
}

function connectedProvider(provider) {
  if (provider?.disabled_at) return false;
  if (provider?.validation_status && provider.validation_status !== 'valid') return false;
  if (!provider?.expires_at) return true;
  const expiresAt = new Date(provider.expires_at).getTime();
  return !Number.isFinite(expiresAt) || expiresAt > Date.now();
}

function activeRoute(route) {
  return route?.active !== false && route?.is_active !== false && Boolean(route?.inbound_did);
}

function idxConnected(idx) {
  const status = String(idx?.status || idx?.connection_status || '').toLowerCase();
  const sources = Array.isArray(idx?.sources) ? idx.sources : Array.isArray(idx?.providers) ? idx.providers : [];
  return ['active', 'connected', 'healthy', 'ready', 'degraded'].includes(status)
    || sources.some((source) => ['active', 'connected', 'healthy', 'ready', 'degraded'].includes(
      String(source?.health || source?.status || '').toLowerCase(),
    ));
}

function CapabilityStatus({ status }) {
  const resolved = status || STATUS.setup;
  return <span className={styles.status} data-tone={resolved.tone}>{resolved.label}</span>;
}

function CapabilityLedger({ id, eyebrow, title, description, items, onActivate = () => {} }) {
  return (
    <section className={styles.ledger} aria-labelledby={`${id}-title`}>
      <header className={styles.sectionHead}>
        <div>
          <span className={styles.kicker}>{eyebrow}</span>
          <h2 id={`${id}-title`}>{title}</h2>
          {description ? <p>{description}</p> : null}
        </div>
        <span className={styles.ledgerCount}>{items.length}</span>
      </header>
      <ul className={styles.capabilityList}>
        {items.map((item) => {
          const Icon = item.Icon || CircleDashed;
          const content = (
            <>
              <span className={styles.capabilityIcon}><Icon aria-hidden="true" /></span>
              <div>
                <strong>{item.name}</strong>
                <p>{item.detail}</p>
              </div>
              <CapabilityStatus status={item.status} />
              {item.href ? <ArrowUpRight className={styles.capabilityArrow} aria-hidden="true" /> : null}
            </>
          );
          return (
            <li key={item.name}>
              {item.href ? (
                <button type="button" className={styles.capabilityLink} onClick={() => onActivate(item.href)}>
                  {content}
                </button>
              ) : content}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function MetricRail({ items }) {
  return (
    <dl className={styles.metrics}>
      {items.map((item) => (
        <div key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
          {item.detail ? <small>{item.detail}</small> : null}
        </div>
      ))}
    </dl>
  );
}

function ActionPanel({ id, eyebrow, title, description, actions, onPrompt, onNavigate }) {
  return (
    <section className={styles.actionPanel} aria-labelledby={`${id}-actions-title`}>
      <header className={styles.sectionHead}>
        <div>
          <span className={styles.kicker}>{eyebrow}</span>
          <h2 id={`${id}-actions-title`}>{title}</h2>
          <p>{description}</p>
        </div>
      </header>
      <div className={styles.actionList}>
        {actions.map((action) => {
          const Icon = action.Icon || Sparkles;
          return (
            <button
              key={action.label}
              type="button"
              onClick={() => action.destination ? onNavigate(action.destination) : onPrompt(action.prompt)}
            >
              <Icon aria-hidden="true" />
              <span><strong>{action.label}</strong><small>{action.detail}</small></span>
              <ArrowUpRight aria-hidden="true" />
            </button>
          );
        })}
      </div>
      <p className={styles.approvalNote}><ShieldCheck aria-hidden="true" /> Prompts open as drafts. Outreach and record-changing actions still require review.</p>
    </section>
  );
}

function NestedFallback() {
  return <div className={styles.nestedFallback} aria-label="Loading AI workspace"><span /><span /><span /></div>;
}

export default function OurAITab({
  onNavigate = () => {},
  salesRoute = null,
  onSalesNavigate = () => {},
}) {
  const [workspace, setWorkspace] = useState(savedWorkspace);
  const [snapshot, setSnapshot] = useState({
    values: {}, errors: {}, loading: true, refreshing: false, updatedAt: null,
  });
  const requestRef = useRef(0);
  const { requestCommand, setOpen } = useAssistant();

  const load = useCallback(async () => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setSnapshot((current) => ({
      ...current,
      loading: Object.keys(current.values).length === 0,
      refreshing: Object.keys(current.values).length > 0,
    }));
    const settled = await Promise.allSettled(SOURCES.map((source) => (
      crmGet(source.path, { retries: 0, timeout: 15_000 })
    )));
    if (requestRef.current !== requestId) return;
    const values = {};
    const errors = {};
    settled.forEach((result, index) => {
      const source = SOURCES[index];
      if (result.status === 'fulfilled') {
        const selected = source.select(result.value);
        values[source.id] = Array.isArray(selected) ? selected : selected || {};
      } else {
        errors[source.id] = result.reason;
      }
    });
    setSnapshot({ values, errors, loading: false, refreshing: false, updatedAt: new Date() });
  }, []);

  useEffect(() => {
    const initial = Promise.resolve().then(load);
    return () => {
      requestRef.current += 1;
      void initial;
    };
  }, [load]);

  useEffect(() => {
    const onTourWorkspace = (event) => {
      const next = String(event.detail || '');
      if (!WORKSPACES.some((item) => item.id === next)) return;
      setWorkspace(next);
      window.sessionStorage.setItem(WORKSPACE_KEY, next);
    };
    window.addEventListener('oracle:ai-workspace', onTourWorkspace);
    return () => window.removeEventListener('oracle:ai-workspace', onTourWorkspace);
  }, []);

  const data = useMemo(() => {
    const values = snapshot.values;
    const contacts = Array.isArray(values.contacts) ? values.contacts : [];
    const clients = Array.isArray(values.clients) ? values.clients : [];
    const commands = Array.isArray(values.commands) ? values.commands : [];
    const providers = Array.isArray(values.providers) ? values.providers : [];
    const routes = Array.isArray(values.routes) ? values.routes : [];
    const calls = Array.isArray(values.calls) ? values.calls : [];
    const threads = Array.isArray(values.threads) ? values.threads : [];
    const transactions = Array.isArray(values.transactions) ? values.transactions : [];
    const sites = Array.isArray(values.sites) ? values.sites : [];
    const segments = Array.isArray(values.segments) ? values.segments : [];
    const homeowners = clients.filter((client) => {
      const type = String(client.client_type || client.type || '').toLowerCase();
      return type.includes('seller') || type.includes('homeowner');
    });
    const stewarded = clients.filter((client) => (
      client?.automation?.enabled === true || client?.ai_automation?.enabled === true
    ));
    return {
      contacts,
      clients,
      commands,
      providers,
      routes,
      calls,
      threads,
      transactions,
      sites,
      segments,
      homeowners,
      stewarded,
      activeRoutes: routes.filter(activeRoute),
      connectedProviders: providers.filter(connectedProvider),
      pendingCommands: commands.filter((command) => ['draft', 'awaiting_approval', 'pending'].includes(command?.state)),
      assistantEnabled: values.assistant?.enabled === true,
      idxReady: idxConnected(values.idx),
    };
  }, [snapshot.values]);

  const sourceState = useCallback((sourceId, active = true, partial = false) => {
    if (snapshot.errors[sourceId]) return STATUS.offline;
    if (!hasOwn(snapshot.values, sourceId)) return STATUS.checking;
    if (active) return partial ? STATUS.partial : STATUS.live;
    return STATUS.setup;
  }, [snapshot.errors, snapshot.values]);

  const selectWorkspace = useCallback((next) => {
    setWorkspace(next);
    window.sessionStorage.setItem(WORKSPACE_KEY, next);
    onSalesNavigate(next === 'sales' ? '/our-ai/sales' : '/our-ai');
  }, [onSalesNavigate]);

  const moveWorkspaceFocus = useCallback((event, index) => {
    let target = null;
    if (event.key === 'ArrowRight') target = (index + 1) % WORKSPACES.length;
    if (event.key === 'ArrowLeft') target = (index - 1 + WORKSPACES.length) % WORKSPACES.length;
    if (event.key === 'Home') target = 0;
    if (event.key === 'End') target = WORKSPACES.length - 1;
    if (target === null) return;
    event.preventDefault();
    document.getElementById(`ai-workspace-tab-${WORKSPACES[target].id}`)?.focus();
  }, []);

  const stagePrompt = useCallback((prompt) => {
    requestCommand({ rawText: prompt });
    setOpen(true);
  }, [requestCommand, setOpen]);

  const liveSourceCount = SOURCES.filter((source) => hasOwn(snapshot.values, source.id)).length;
  const sourceErrors = SOURCES.filter((source) => snapshot.errors[source.id]);
  const routedWorkspace = salesRoute?.startsWith('/our-ai/sales') ? 'sales' : workspace;
  const activeWorkspace = WORKSPACES.find((item) => item.id === routedWorkspace) || WORKSPACES[0];

  const coworkItems = [
    {
      name: 'Cowork',
      detail: 'Conversational CRM research, record context, files, public-source search, safe updates, and undoable actions.',
      status: sourceState('assistant', data.assistantEnabled),
      Icon: Bot,
    },
    {
      name: 'Agentic Real Estate CRM',
      detail: 'Contact truth, opportunity signals, conversations, tasks, listings, client records, and AI tools share one tenant-safe context.',
      status: sourceState('clients'),
      Icon: Users,
    },
    {
      name: 'Transaction Management',
      detail: 'Transactions, offers, milestones, closing controls, contract generation, encrypted vault records, and compliance review.',
      status: sourceState('transactions'),
      Icon: FileCheck2,
    },
    {
      name: 'Back Office',
      detail: 'Deal reporting, approval queues, document holdings, and operational controls are live; commission accounting is not yet connected.',
      status: sourceState('transactions', true, true),
      Icon: BriefcaseBusiness,
    },
    {
      name: 'Mobile Workspace',
      detail: 'The full CRM is responsive and touch-ready. A separately distributed native App Store / Play Store build still requires setup.',
      status: STATUS.partial,
      Icon: Smartphone,
    },
  ];

  const salesStatus = (state) => ({
    live: STATUS.live,
    setup_required: STATUS.setup,
    partial: STATUS.partial,
    disabled: STATUS.disabled,
  }[state] || STATUS.checking);
  const salesIcons = {
    'sales-agent': PhoneCall,
    'power-dialer': PhoneCall,
    'smart-plans': Workflow,
    'provider-delivery': PlugZap,
    'lead-routing': Route,
  };
  const backendSalesItems = Array.isArray(snapshot.values.sales?.capabilities)
    ? snapshot.values.sales.capabilities
    : [];
  const fallbackSalesItems = [
    {
      name: 'Sales Agent',
      detail: 'Qualifies buyers and sellers, drafts personalized follow-up, summarizes conversations, and hands context back to the agent.',
      status: sourceState('threads', data.assistantEnabled),
      Icon: PhoneCall,
    },
    {
      name: 'Power Dialer',
      detail: 'Inbound AI voice, disclosure, routing, call records, scripts, and contact matching are available when a verified telephony route is active.',
      status: sourceState('routes', data.activeRoutes.length > 0),
      Icon: PhoneCall,
    },
    {
      name: 'Smart Plans',
      detail: 'AI nurture, staged email/call/calendar actions, client reconciliation, and approval queues are live. A visual multi-step plan builder is partial.',
      status: sourceState('commands', true, true),
      Icon: Workflow,
    },
    {
      name: 'Provider delivery',
      detail: 'Email, calendar, SMS, and calling use tenant-scoped provider credentials; no channel is represented as connected without a valid credential.',
      status: sourceState('providers', data.connectedProviders.length > 0),
      Icon: PlugZap,
    },
    {
      name: 'Lead Routing',
      detail: 'Signed multi-source capture, exact contact deduplication, ZIP and intent rules, capacity-aware assignment, and observed source analytics.',
      status: sourceState('routing'),
      Icon: Route,
    },
  ];
  const salesItems = backendSalesItems.length > 0
    ? backendSalesItems.map((item) => ({
        name: item.name,
        detail: item.description,
        status: salesStatus(item.state),
        Icon: salesIcons[item.id] || CircleDashed,
        href: item.href,
      }))
    : fallbackSalesItems.map((item, index) => ({
        ...item,
        href: [
          '/our-ai/sales/agent',
          '/our-ai/sales/dialer',
          '/our-ai/sales/plans',
          '/our-ai/sales/providers',
          '/our-ai/sales/routing',
        ][index],
      }));

  const socialItems = [
    {
      name: 'Social Agent',
      detail: 'NEOH can research a market, prepare a weekly calendar, and draft listing, neighborhood, and educational content for review.',
      status: data.assistantEnabled ? STATUS.ready : sourceState('assistant', false),
      Icon: Sparkles,
    },
    {
      name: 'Social Studio',
      detail: 'Cross-channel scheduling, publishing, analytics, and autopilot need social account connectors before they can run.',
      status: STATUS.setup,
      Icon: Share2,
    },
    {
      name: 'Multi-Channel Advertising',
      detail: 'Google, Microsoft, Facebook, and Instagram campaign management and attribution require ad-network accounts and billing.',
      status: STATUS.setup,
      Icon: Megaphone,
    },
    {
      name: 'OpenAI Ads',
      detail: 'Conversational ad placement, managed creative, landing capture, and conversion attribution are not connected in this environment.',
      status: STATUS.setup,
      Icon: MessageSquare,
    },
    {
      name: 'Listing Blast',
      detail: 'One-click listing, price-drop, open-house, just-listed, and just-sold campaign publishing needs email and ad connectors.',
      status: STATUS.setup,
      Icon: Megaphone,
    },
    {
      name: 'Own Your ZIP Code',
      detail: 'Exclusive ZIP farming across social ads and direct-mail postcards requires audience, print, and media-buy integrations.',
      status: STATUS.setup,
      Icon: MapPin,
    },
    {
      name: 'Social Ads',
      detail: 'Sphere, dynamic MLS, behavioral, and retargeting campaigns need Meta advertising authorization and spend controls.',
      status: STATUS.setup,
      Icon: Share2,
    },
    {
      name: 'Google LSA',
      detail: 'Google Screened onboarding, review management, budgets, disputes, and lead sync require a verified Local Services Ads account.',
      status: STATUS.setup,
      Icon: Search,
    },
  ];

  const homeownerItems = [
    {
      name: 'Homeowner Agent',
      detail: 'Mines seller and homeowner records, scores intent, reconciles evidence, recommends next actions, and preserves a human handoff.',
      status: sourceState('clients'),
      Icon: House,
    },
    {
      name: 'Seller audiences',
      detail: 'Saved segments, seller stages, property candidates, equity context, and nurture gaps can be reviewed from the People workspace.',
      status: sourceState('segments'),
      Icon: Users,
    },
    {
      name: 'Customer Search App',
      detail: 'A Closely-style branded customer app with alerts, chat, saved homes, and HomeGPT is not yet distributed from Neoh.',
      status: STATUS.setup,
      Icon: Smartphone,
    },
  ];

  const automationItems = [
    {
      name: 'Custom Agents',
      detail: 'Natural-language commands, per-client AI stewardship, agent settings, and review-gated execution are live; reusable trigger templates are partial.',
      status: sourceState('commands', true, true),
      Icon: Bot,
    },
    {
      name: 'Approval orchestration',
      detail: 'Research and drafting can run autonomously while outreach, calls, calendar writes, legal documents, and financial actions stay review-gated.',
      status: sourceState('commands'),
      Icon: ShieldCheck,
    },
    {
      name: 'Channel providers',
      detail: 'Google, ACS, SES, and Twilio credentials are tenant-scoped, encrypted, revocable, and never exposed in the UI.',
      status: sourceState('providers', data.connectedProviders.length > 0),
      Icon: PlugZap,
    },
  ];

  const siteItems = [
    {
      name: 'Hyperlocal Website',
      detail: 'Build source-backed area sites, private revisions, IDX-aware pages, lead intake, attribution, and approval-gated publishing.',
      status: sourceState('sites'),
      Icon: Globe2,
    },
    {
      name: 'MLS / IDX',
      detail: 'Direct authorized MLS/RESO health is checked before IDX can be enabled; third-party listing aggregators are not treated as MLS.',
      status: sourceState('idx', data.idxReady),
      Icon: Search,
    },
    {
      name: 'WordPress IDX Plugin',
      detail: 'A packaged WordPress plugin with property search, listing pages, capture, and CRM sync still requires implementation and distribution.',
      status: STATUS.setup,
      Icon: PlugZap,
    },
    {
      name: 'Website Design',
      detail: 'Brand, service areas, trust details, responsive preview, SEO metadata, and publish approval are available in the builder below.',
      status: sourceState('sites'),
      Icon: Globe2,
    },
    {
      name: 'AEO',
      detail: 'Answer pages, structured identity, citation monitoring, and answer-engine reporting need a dedicated publishing and measurement layer.',
      status: STATUS.setup,
      Icon: Search,
    },
    {
      name: 'GEO',
      detail: 'AI-crawler controls, answer-ready content, and generative-engine visibility monitoring are not connected yet.',
      status: STATUS.setup,
      Icon: Sparkles,
    },
    {
      name: 'SEO',
      detail: 'Per-site titles and descriptions are live. Technical audits, rank tracking, link work, and managed local SEO remain setup work.',
      status: STATUS.partial,
      Icon: Search,
    },
  ];

  const coworkActions = [
    { label: 'Run today’s revenue brief', detail: 'Rank the next five actions using live CRM evidence.', prompt: 'Build my revenue brief for today. Rank the five highest-value actions using current CRM, conversation, deal, and deadline evidence. Cite the records used and do not send anything.', Icon: Sparkles },
    { label: 'Research a property', detail: 'Use public and authorized property sources with citations.', prompt: 'Help me research a property using public or authorized sources only. Ask me for the address, then return cited facts, unknowns, and the next verification step.', Icon: Search },
    { label: 'Review pending actions', detail: `${data.pendingCommands.length} command drafts currently need review.`, prompt: 'Summarize my pending AI approvals by risk and urgency. Do not execute, send, call, schedule, or approve anything.', Icon: ShieldCheck },
    { label: 'Open transactions', detail: 'Go directly to offers, milestones, contracts, and closings.', destination: 'deals', Icon: BriefcaseBusiness },
  ];

  const salesActions = [
    { label: 'Build a call list', detail: 'Prioritize contacts from CRM evidence and explain the ranking.', prompt: 'Build a prioritized call list from my current CRM. Include the reason, last touch, likely intent, and a suggested opening for each person. Do not place calls.', Icon: PhoneCall },
    { label: 'Draft an outreach sequence', detail: 'Prepare text, email, and call steps for approval.', prompt: 'Draft a concise multi-channel follow-up sequence for my highest-priority unresponsive leads. Personalize from verified CRM facts and stage every outbound step for approval.', Icon: Mail },
    { label: 'Find unanswered conversations', detail: `${data.threads.length} conversation threads are available to inspect.`, prompt: 'Review current conversation threads and identify the highest-priority unanswered or stalled conversations. Explain why each needs attention and draft replies without sending.', Icon: MessageSquare },
    { label: 'Open people', detail: 'Review contacts, opportunities, AI stewardship, and intake.', destination: 'people', Icon: Users },
  ];

  const socialActions = [
    { label: 'Build a weekly content plan', detail: 'Create a review-ready calendar from local market facts.', prompt: 'Create a one-week real estate social content plan for my service area using verified local market facts. Include channel, format, hook, source, CTA, and compliance note. Do not publish.', Icon: CalendarDays },
    { label: 'Create a listing campaign', detail: 'Draft listing, email, and social creative without publishing.', prompt: 'Ask me which listing to use, then draft a coordinated campaign for social and email. Include just-listed, open-house, price-update, and follow-up variants. Do not publish or buy media.', Icon: Megaphone },
    { label: 'Turn a market brief into posts', detail: 'Translate cited research into channel-specific drafts.', prompt: 'Build a cited local market brief, then adapt it into review-ready posts for Instagram, Facebook, LinkedIn, YouTube Shorts, and Google Business Profile. Do not publish.', Icon: Share2 },
  ];

  const homeownerActions = [
    { label: 'Find seller intent', detail: 'Rank possible seller signals and show the evidence.', prompt: 'Analyze my homeowner and seller records for likely intent signals. Rank opportunities, cite the CRM and public-property evidence, state uncertainty, and do not contact anyone.', Icon: House },
    { label: 'Prepare equity reviews', detail: 'Draft a useful homeowner check-in for approval.', prompt: 'Identify homeowners who may benefit from an annual property and equity review. Draft a factual, low-pressure check-in for each and do not send it.', Icon: FileCheck2 },
    { label: 'Audit nurture gaps', detail: `${data.segments.length} saved audience segments are available.`, prompt: 'Audit my seller and homeowner nurture coverage. Identify records without a clear next action, missing facts, stale follow-up, or automation gaps. Do not change records.', Icon: Workflow },
    { label: 'Open opportunities', detail: 'Inspect AI score, stage, evidence, next action, and candidates.', destination: 'people', Icon: Users },
  ];

  return (
    <section className={styles.wrap} aria-labelledby="our-ai-title" aria-busy={snapshot.loading || snapshot.refreshing}>
      <header className={styles.hero}>
        <div>
          <span className={styles.kicker}>Agentic real estate operating system</span>
          <h1 id="our-ai-title">Our AI</h1>
          <p>One command center for NEOH’s cowork, sales, homeowner, automation, social, and web capabilities—with real connection states and human approval boundaries.</p>
          <div className={styles.readiness} aria-label="AI readiness summary">
            <span data-tone={data.assistantEnabled ? 'good' : 'neutral'}><Bot aria-hidden="true" /> {data.assistantEnabled ? 'NEOH online' : 'NEOH setup'}</span>
            <span data-tone={sourceErrors.length ? 'warn' : 'good'}><CheckCircle2 aria-hidden="true" /> {liveSourceCount}/{SOURCES.length} sources</span>
            <span data-tone={data.pendingCommands.length ? 'warn' : 'good'}><ShieldCheck aria-hidden="true" /> {data.pendingCommands.length} awaiting review</span>
          </div>
        </div>
        <div className={styles.heroActions}>
          <button type="button" className={styles.refresh} onClick={load} disabled={snapshot.loading || snapshot.refreshing} aria-label="Refresh Our AI status">
            <RefreshCw aria-hidden="true" />
          </button>
          <button type="button" className={styles.ask} onClick={() => setOpen(true)}>
            <Sparkles aria-hidden="true" /> Ask NEOH
          </button>
        </div>
      </header>

      {sourceErrors.length > 0 ? (
        <div className={styles.sourceNotice} role="status">
          <CircleDashed aria-hidden="true" />
          <div>
            <strong>Partial live view</strong>
            <p>{sourceErrors.map((source) => source.label).join(', ')} {sourceErrors.length === 1 ? 'is' : 'are'} temporarily unavailable. No unavailable capability is being shown as active.</p>
          </div>
          <button type="button" onClick={load}>Retry</button>
        </div>
      ) : null}

      <nav className={styles.workspaceNav} aria-label="Our AI capabilities">
        <div role="tablist" aria-orientation="horizontal">
          {WORKSPACES.map((item, index) => {
            const Icon = item.Icon;
            const selected = item.id === activeWorkspace.id;
            return (
              <button
                key={item.id}
                id={`ai-workspace-tab-${item.id}`}
                type="button"
                role="tab"
                aria-selected={selected}
                aria-controls={`ai-workspace-panel-${item.id}`}
                tabIndex={selected ? 0 : -1}
                onClick={() => selectWorkspace(item.id)}
                onKeyDown={(event) => moveWorkspaceFocus(event, index)}
              >
                <Icon aria-hidden="true" />
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      </nav>

      <div
        id={`ai-workspace-panel-${activeWorkspace.id}`}
        className={styles.workspace}
        role="tabpanel"
        aria-labelledby={`ai-workspace-tab-${activeWorkspace.id}`}
      >
        {activeWorkspace.id === 'intelligence' ? (
          <Suspense fallback={null}>
            <IntelligenceFeed />
          </Suspense>
        ) : activeWorkspace.id === 'cowork' ? (
          <>
            <MetricRail items={[
              { label: 'CRM relationships', value: formatNumber(data.contacts.length || data.clients.length), detail: `${formatNumber(data.clients.length)} opportunities` },
              { label: 'Conversations', value: formatNumber(data.threads.length), detail: 'live threads' },
              { label: 'Transactions', value: formatNumber(data.transactions.length), detail: 'offers to close' },
              { label: 'Provider links', value: formatNumber(data.connectedProviders.length), detail: 'valid credentials' },
            ]} />
            <div className={styles.split}>
              <ActionPanel id="cowork" eyebrow="Command NEOH" title="Work across the business" description="Start from a business outcome. NEOH gathers context, shows sources, and stages consequential actions for review." actions={coworkActions} onPrompt={stagePrompt} onNavigate={onNavigate} />
              <CapabilityLedger id="cowork-ledger" eyebrow="Live operating map" title="Core platform" description="Actual backend and provider state—not a marketing checklist." items={coworkItems} />
            </div>
          </>
        ) : null}

        {activeWorkspace.id === 'sales' ? (
          salesRoute && salesRoute !== '/our-ai/sales' ? (
            <Suspense fallback={<NestedFallback />}>
              <SalesWorkspace route={salesRoute} onNavigate={onSalesNavigate} />
            </Suspense>
          ) : <>
            <MetricRail items={[
              { label: 'Contacts', value: formatNumber(data.contacts.length), detail: 'canonical identities' },
              { label: 'Open conversations', value: formatNumber(data.threads.length), detail: 'email, text, and calls' },
              { label: 'Voice routes', value: formatNumber(data.activeRoutes.length), detail: data.activeRoutes.length ? 'verified inbound' : 'setup required' },
              { label: 'Recorded calls', value: formatNumber(data.calls.length), detail: 'inbound sessions' },
              { label: 'Routed leads', value: formatNumber(snapshot.values.routing?.totals?.routed), detail: 'last 30 days' },
            ]} />
            <div className={styles.split}>
              <ActionPanel id="sales" eyebrow="Pipeline execution" title="Qualify and follow up" description="Use CRM truth to prioritize outreach, prepare scripts, summarize conversations, and keep every send or call reviewable." actions={salesActions} onPrompt={stagePrompt} onNavigate={onNavigate} />
              <CapabilityLedger id="sales-ledger" eyebrow="Sales stack" title="Sales capabilities" items={salesItems} onActivate={onSalesNavigate} />
            </div>
          </>
        ) : null}

        {activeWorkspace.id === 'social' ? (
          <div className={styles.split}>
            <ActionPanel id="social" eyebrow="Content intelligence" title="Plan before publishing" description="Research and creation are available now. Channel publishing and paid-media controls remain explicitly disconnected until accounts are linked." actions={socialActions} onPrompt={stagePrompt} onNavigate={onNavigate} />
            <CapabilityLedger id="social-ledger" eyebrow="Organic + paid" title="Social and lead generation" description="Every channel from the requested solution set is represented below." items={socialItems} />
          </div>
        ) : null}

        {activeWorkspace.id === 'homeowners' ? (
          <>
            <MetricRail items={[
              { label: 'Seller / homeowner', value: formatNumber(data.homeowners.length), detail: 'identified records' },
              { label: 'AI stewardship', value: formatNumber(data.stewarded.length), detail: 'enabled clients' },
              { label: 'Saved audiences', value: formatNumber(data.segments.length), detail: 'reusable filters' },
              { label: 'All opportunities', value: formatNumber(data.clients.length), detail: 'buyer and seller' },
            ]} />
            <div className={styles.split}>
              <ActionPanel id="homeowners" eyebrow="Seller intelligence" title="Develop homeowner opportunity" description="Use verified CRM and property evidence to spot intent, maintain value, and prepare a respectful human handoff." actions={homeownerActions} onPrompt={stagePrompt} onNavigate={onNavigate} />
              <CapabilityLedger id="homeowner-ledger" eyebrow="Homeowner lifecycle" title="Homeowner capabilities" items={homeownerItems} />
            </div>
          </>
        ) : null}

        {activeWorkspace.id === 'automations' ? (
          <>
            <CapabilityLedger id="automation-ledger" eyebrow="Governed autonomy" title="Automations and custom agents" description="Configure the real autonomy boundary, provider links, action queue, contract holdings, and agent onboarding below." items={automationItems} />
            <div className={styles.embeddedWorkspace}>
              <Suspense fallback={<NestedFallback />}><PersonalAITab /></Suspense>
            </div>
          </>
        ) : null}

        {activeWorkspace.id === 'sites' ? (
          <>
            <CapabilityLedger id="sites-ledger" eyebrow="Owned audience" title="Web, IDX, and search presence" description="The hyperlocal builder is live. Adjacent distribution and visibility products remain visibly separated until implemented." items={siteItems} />
            <div className={styles.embeddedWorkspace}>
              <Suspense fallback={<NestedFallback />}><StudioTab embedded /></Suspense>
            </div>
          </>
        ) : null}
      </div>
    </section>
  );
}
