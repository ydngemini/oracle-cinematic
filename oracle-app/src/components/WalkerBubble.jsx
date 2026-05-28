import { useState, useEffect, useRef } from 'react';
import { useOracleState } from '../state';
import styles from './WalkerBubble.module.css';

const AGENT_COLORS = {
  SCOUT: '#ffb800',
  ANALYST: '#00d4ff',
  CLOSER: '#ff4da6',
  LEGAL: '#7cff6b',
};

export function WalkerBubble() {
  const { walkerThought, walkerAgent, walkerStreaming } = useOracleState();
  const [visible, setVisible] = useState(false);
  const hideTimer = useRef(null);

  useEffect(() => {
    if (walkerStreaming) {
      setVisible(true);
      clearTimeout(hideTimer.current);
    } else if (walkerThought) {
      hideTimer.current = setTimeout(() => setVisible(false), 4000);
    }
    return () => clearTimeout(hideTimer.current);
  }, [walkerStreaming, walkerThought]);

  const color = AGENT_COLORS[walkerAgent] || '#ffb800';

  if (!visible && !walkerStreaming) return null;

  return (
    <div className={styles.bubble} data-visible={visible} data-streaming={walkerStreaming}>
      <div className={styles.header}>
        <span className={styles.dot} style={{ background: color, boxShadow: `0 0 8px ${color}` }} />
        <span className={styles.agentName} style={{ color }}>
          {walkerAgent}
        </span>
        {walkerStreaming && <span className={styles.streamIndicator} />}
      </div>
      <div className={styles.thought}>
        {walkerThought}
        {walkerStreaming && <span className={styles.cursor}>|</span>}
      </div>
    </div>
  );
}
