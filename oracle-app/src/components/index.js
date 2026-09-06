export { CrmShell } from './CrmShell';
// Tab views (Houses, Portfolio, ClientCrmTab, CommsTab,
// MyProfileTab) are intentionally NOT re-exported: CrmShell lazy-loads them
// per-tab, and a static re-export would defeat the code-split.
export { AgentStatusBar } from './AgentStatusBar';
export { PropertySpecs } from './PropertySpecs';
export { LiveTranscript } from './LiveTranscript';
export { LivePulse } from './LivePulse';
// The tour viewers (gsplat / PlayCanvas / PanoViewer) are intentionally NOT
// re-exported here — they are reached through TourViewer's lazy imports so
// their engines stay out of the initial CRM chunk.
export { LoginVault } from './LoginVault';
export { BillingOverlay } from './BillingOverlay';
export { SubscriptionBadge } from './SubscriptionBadge';
export { ErrorBoundary } from './ErrorBoundary';
export { OnboardingGate } from './OnboardingGate';
export { DossierPanel } from './DossierPanel';
