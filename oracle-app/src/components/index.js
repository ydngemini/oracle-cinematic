export { DashboardLayout } from './DashboardLayout';
export { CrmShell } from './CrmShell';
// Tab views (MarketplaceTab, HouseProfileTab, ClientCrmTab, CommsTab,
// MyProfileTab) are intentionally NOT re-exported: CrmShell lazy-loads them
// per-tab, and a static re-export would defeat the code-split.
export { AgentStatusBar } from './AgentStatusBar';
export { PropertySpecs } from './PropertySpecs';
export { LiveTranscript } from './LiveTranscript';
export { LivePulse } from './LivePulse';
// PropertyCanvas is intentionally NOT re-exported here: it is lazy-loaded
// directly in DashboardLayout to keep gsplat out of the initial chunk. A static
// re-export would defeat the code-split (Vite INEFFECTIVE_DYNAMIC_IMPORT).
export { LoginVault } from './LoginVault';
export { BillingOverlay } from './BillingOverlay';
export { WalkerBubble } from './WalkerBubble';
export { SubscriptionBadge } from './SubscriptionBadge';
export { ErrorBoundary } from './ErrorBoundary';
export { OnboardingGate } from './OnboardingGate';
export { PipelineBoard } from './PipelineBoard';
export { DossierPanel } from './DossierPanel';
