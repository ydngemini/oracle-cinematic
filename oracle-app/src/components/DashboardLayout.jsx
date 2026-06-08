import { AgentStatusBar } from './AgentStatusBar';
import { PropertySpecs } from './PropertySpecs';
import { LiveTranscript } from './LiveTranscript';
import { PropertyCanvas } from './PropertyCanvas';
import { WalkerBubble } from './WalkerBubble';
import { DealPipeline } from './DealPipeline';
import { BillingOverlay } from './BillingOverlay';
import { SubscriptionBadge } from './SubscriptionBadge';
import styles from './DashboardLayout.module.css';

export function DashboardLayout() {
  return (
    <div className={styles.viewport}>
      <PropertyCanvas />

      <div className={styles.hud}>
        <div className={styles.topBar}>
          <AgentStatusBar />
          <SubscriptionBadge />
        </div>

        <div className={styles.leftPanel}>
          <PropertySpecs />
        </div>

        <div className={styles.rightPanel}>
          <DealPipeline />
        </div>

        <div className={styles.bottomPanel}>
          <LiveTranscript />
        </div>
      </div>

      <WalkerBubble />
      <BillingOverlay />
    </div>
  );
}
