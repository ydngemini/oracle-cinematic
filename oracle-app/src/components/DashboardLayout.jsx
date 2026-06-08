import { AgentStatusBar } from './AgentStatusBar';
import { PropertySpecs } from './PropertySpecs';
import { LiveTranscript } from './LiveTranscript';
import { PropertyCanvas } from './PropertyCanvas';
import { WalkerBubble } from './WalkerBubble';
import { DealPipeline } from './DealPipeline';
import styles from './DashboardLayout.module.css';

export function DashboardLayout() {
  return (
    <div className={styles.viewport}>
      <PropertyCanvas />

      <div className={styles.hud}>
        <div className={styles.topBar}>
          <AgentStatusBar />
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
    </div>
  );
}
