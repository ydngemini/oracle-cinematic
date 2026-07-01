import { useAlertContext } from './AlertSystem';
import './AlertIndicator.css';

const SEVERITY_CONFIG = {
  warning: { color: '#ffaa00', label: 'WARN', icon: '⚠' },
  critical: { color: '#ff4444', label: 'CRIT', icon: '⚡' },
  breach: { color: '#ff0066', label: 'BREACH', icon: '⚠' }
};

export default function AlertIndicator() {
  const { getAlertCounts, alerts } = useAlertContext();
  const counts = getAlertCounts();
  const hasActiveAlerts = counts.total > 0;

  return (
    <div className="alert-indicator">
      <div className={`led-bar ${hasActiveAlerts ? 'active' : ''}`}>
        <div className="led-group">
          <div 
            className={`led warning ${counts.warning > 0 ? 'lit' : ''}`}
            style={{ '--led-color': SEVERITY_CONFIG.warning.color }}
          />
          <span className="led-count">{counts.warning}</span>
          <span className="led-label">WARN</span>
        </div>
        
        <div className="led-group">
          <div 
            className={`led critical ${counts.critical > 0 ? 'lit' : ''}`}
            style={{ '--led-color': SEVERITY_CONFIG.critical.color }}
          />
          <span className="led-count">{counts.critical}</span>
          <span className="led-label">CRIT</span>
        </div>
        
        <div className="led-group">
          <div 
            className={`led breach ${counts.breach > 0 ? 'lit' : ''}`}
            style={{ '--led-color': SEVERITY_CONFIG.breach.color }}
          />
          <span className="led-count">{counts.breach}</span>
          <span className="led-label">BREACH</span>
        </div>
      </div>
      
      {hasActiveAlerts && (
        <div className="alert-flash">
          <span className="flash-text">
            {counts.breach > 0 ? 'BREACH DETECTED' : 
             counts.critical > 0 ? 'CRITICAL ALERT' : 
             `${counts.warning} WARNING${counts.warning > 1 ? 'S' : ''}`}
          </span>
        </div>
      )}
    </div>
  );
}
