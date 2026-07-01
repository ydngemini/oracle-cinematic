import { useState } from 'react';
import styles from './DriveOverlay.module.css';

const actions = [
  { id: 'scale-up', label: 'SCALE UP', icon: '⬆', color: '#00ff88', confirm: true },
  { id: 'scale-down', label: 'SCALE DOWN', icon: '⬇', color: '#ff9f1a', confirm: true },
  { id: 'restart', label: 'RESTART', icon: '↻', color: '#00d4ff', confirm: true },
  { id: 'stop', label: 'STOP', icon: '■', color: '#ff3b5c', confirm: true },
  { id: 'snapshot', label: 'SNAPSHOT', icon: '◉', color: '#a855f7', confirm: false },
  { id: 'logs', label: 'VIEW LOGS', icon: '☰', color: '#00d4ff', confirm: false },
];

export default function DriveOverlay({ selectedResource, onAction, disabled = false }) {
  const [pendingAction, setPendingAction] = useState(null);
  const [showConfirm, setShowConfirm] = useState(false);
  const [actionStatus, setActionStatus] = useState(null);

  const handleActionClick = (action) => {
    if (disabled) return;

    if (action.confirm) {
      setPendingAction(action);
      setShowConfirm(true);
    } else {
      executeAction(action);
    }
  };

  const executeAction = async (action) => {
    setActionStatus({ type: 'loading', action });
    setShowConfirm(false);
    
    try {
      await onAction?.(action.id, selectedResource);
      setActionStatus({ type: 'success', action });
      setTimeout(() => setActionStatus(null), 2000);
    } catch (error) {
      setActionStatus({ type: 'error', action, error });
      setTimeout(() => setActionStatus(null), 3000);
    }
  };

  const handleConfirm = () => {
    if (pendingAction) {
      executeAction(pendingAction);
    }
  };

  const handleCancel = () => {
    setShowConfirm(false);
    setPendingAction(null);
  };

  return (
    <div className={styles.driveOverlay}>
      <div className={styles.overlayHeader}>
        <h3>CONTROLS</h3>
        {selectedResource && (
          <span className={styles.targetInfo}>
            TARGET: {selectedResource.id?.slice(0, 12)}...
          </span>
        )}
      </div>

      <div className={styles.actionGrid}>
        {actions.map(action => (
          <button
            key={action.id}
            className={`${styles.actionBtn} ${disabled || !selectedResource ? styles.disabled : ''}`}
            onClick={() => handleActionClick(action)}
            disabled={disabled || !selectedResource}
            style={{ '--action-color': action.color }}
          >
            <span className={styles.actionIcon}>{action.icon}</span>
            <span className={styles.actionLabel}>{action.label}</span>
          </button>
        ))}
      </div>

      {showConfirm && pendingAction && (
        <div className={styles.confirmOverlay}>
          <div className={styles.confirmDialog}>
            <div className={styles.confirmIcon} style={{ color: pendingAction.color }}>
              {pendingAction.icon}
            </div>
            <h4>CONFIRM ACTION</h4>
            <p>
              Are you sure you want to {pendingAction.label.toLowerCase()}{' '}
              {selectedResource?.id?.slice(0, 16)}...?
            </p>
            <div className={styles.confirmButtons}>
              <button 
                className={styles.confirmBtn}
                onClick={handleConfirm}
                style={{ borderColor: pendingAction.color, color: pendingAction.color }}
              >
                CONFIRM
              </button>
              <button 
                className={styles.cancelBtn}
                onClick={handleCancel}
              >
                CANCEL
              </button>
            </div>
          </div>
        </div>
      )}

      {actionStatus && (
        <div className={`${styles.statusBanner} ${styles[actionStatus.type]}`}>
          <span className={styles.statusIcon}>
            {actionStatus.type === 'loading' && '◌'}
            {actionStatus.type === 'success' && '✓'}
            {actionStatus.type === 'error' && '✗'}
          </span>
          <span className={styles.statusText}>
            {actionStatus.type === 'loading' && `Executing ${actionStatus.action.label}...`}
            {actionStatus.type === 'success' && `${actionStatus.action.label} completed`}
            {actionStatus.type === 'error' && `Failed: ${actionStatus.error}`}
          </span>
        </div>
      )}

      {!selectedResource && (
        <div className={styles.selectPrompt}>
          <span className={styles.promptIcon}>◎</span>
          <span>Select a resource to enable controls</span>
        </div>
      )}
    </div>
  );
}
