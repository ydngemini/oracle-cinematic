import { useAlertContext } from './AlertSystem';
import './AlertHistory.css';

const SEVERITY_STYLES = {
  warning: { border: '#ffaa00', bg: 'rgba(255, 170, 0, 0.1)' },
  critical: { border: '#ff4444', bg: 'rgba(255, 68, 68, 0.15)' },
  breach: { border: '#ff0066', bg: 'rgba(255, 0, 102, 0.2)' }
};

function formatTimestamp(isoString) {
  const date = new Date(isoString);
  return {
    date: date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
    time: date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  };
}

function AlertItem({ alert, onClear }) {
  const { date, time } = formatTimestamp(alert.timestamp);
  const style = SEVERITY_STYLES[alert.severity] || SEVERITY_STYLES.warning;
  
  return (
    <div 
      className={`alert-item ${alert.active ? 'active' : 'resolved'}`}
      style={{ 
        '--border-color': style.border,
        '--bg-color': style.bg
      }}
    >
      <div className="alert-header">
        <span className={`alert-severity ${alert.severity}`}>
          {alert.severity.toUpperCase()}
        </span>
        <span className="alert-resource">{alert.resource}</span>
      </div>
      
      <div className="alert-message">{alert.message}</div>
      
      <div className="alert-footer">
        <span className="alert-timestamp">
          <span className="date">{date}</span>
          <span className="time">{time}</span>
        </span>
        {alert.active && (
          <button className="clear-btn" onClick={() => onClear(alert.id)}>
            ACK
          </button>
        )}
        {alert.resolvedAt && (
          <span className="resolved-badge">RESOLVED</span>
        )}
      </div>
    </div>
  );
}

export default function AlertHistory({ maxItems = 20 }) {
  const { history, clearAlert } = useAlertContext();
  const displayItems = history.slice(0, maxItems);
  
  return (
    <div className="alert-history">
      <div className="history-header">
        <span className="history-title">ALERT HISTORY</span>
        <span className="history-count">{history.length} total</span>
      </div>
      
      <div className="history-scroll">
        {displayItems.length === 0 ? (
          <div className="no-alerts">No alerts recorded</div>
        ) : (
          displayItems.map(alert => (
            <AlertItem 
              key={alert.id} 
              alert={alert} 
              onClear={clearAlert}
            />
          ))
        )}
      </div>
    </div>
  );
}
