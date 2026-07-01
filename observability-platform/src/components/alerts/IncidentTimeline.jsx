import { useMemo } from 'react';
import { useAlertContext } from './AlertSystem';
import './IncidentTimeline.css';

const SEVERITY_COLORS = {
  warning: '#ffaa00',
  critical: '#ff4444',
  breach: '#ff0066'
};

function formatTime(isoString) {
  return new Date(isoString).toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit'
  });
}

function TimelineMarker({ alert, position, isFirst, isLast }) {
  const color = SEVERITY_COLORS[alert.severity] || SEVERITY_COLORS.warning;
  
  return (
    <div 
      className={`timeline-marker ${alert.active ? 'active' : 'resolved'}`}
      style={{ 
        left: `${position}%`,
        '--marker-color': color
      }}
    >
      <div className="marker-dot" />
      <div className="marker-tooltip">
        <div className="tooltip-header">
          <span className="tooltip-severity">{alert.severity.toUpperCase()}</span>
          <span className="tooltip-time">{formatTime(alert.timestamp)}</span>
        </div>
        <div className="tooltip-message">{alert.message}</div>
        <div className="tooltip-resource">{alert.resource}</div>
      </div>
      {isFirst && <div className="marker-label start">START</div>}
      {isLast && <div className="marker-label end">NOW</div>}
    </div>
  );
}

export default function IncidentTimeline({ 
  timeframeMinutes = 60,
  showResolved = true,
  maxMarkers = 20 
}) {
  const { history } = useAlertContext();
  
  const timelineData = useMemo(() => {
    const now = Date.now();
    const startTime = now - (timeframeMinutes * 60 * 1000);
    
    let filtered = history.filter(alert => {
      const alertTime = new Date(alert.timestamp).getTime();
      return alertTime >= startTime;
    });
    
    if (!showResolved) {
      filtered = filtered.filter(a => a.active);
    }
    
    filtered = filtered.slice(0, maxMarkers);
    
    if (filtered.length === 0) {
      return { markers: [], startTime, endTime: now };
    }
    
    const sorted = [...filtered].sort((a, b) => 
      new Date(a.timestamp) - new Date(b.timestamp)
    );
    
    const markers = sorted.map((alert, index) => {
      const alertTime = new Date(alert.timestamp).getTime();
      const position = ((alertTime - startTime) / (now - startTime)) * 100;
      return {
        alert,
        position: Math.max(0, Math.min(100, position)),
        isFirst: index === 0,
        isLast: index === sorted.length - 1
      };
    });
    
    return { markers, startTime, endTime: now };
  }, [history, timeframeMinutes, showResolved, maxMarkers]);

  const minutesAgo = (n) => Math.max(0, 100 - (n / timeframeMinutes) * 100);

  return (
    <div className="incident-timeline">
      <div className="timeline-header">
        <span className="timeline-title">INCIDENT TIMELINE</span>
        <span className="timeline-range">
          Last {timeframeMinutes} min • {timelineData.markers.length} incidents
        </span>
      </div>
      
      <div className="timeline-container">
        <div className="timeline-track">
          <div className="track-line" />
          
          {timelineData.markers.map(({ alert, position, isFirst, isLast }) => (
            <TimelineMarker 
              key={alert.id}
              alert={alert}
              position={position}
              isFirst={isFirst}
              isLast={isLast}
            />
          ))}
        </div>
        
        <div className="timeline-scale">
          <span style={{ left: '0%' }}>-{timeframeMinutes}m</span>
          <span style={{ left: `${minutesAgo(30)}%` }}>-30m</span>
          <span style={{ left: `${minutesAgo(15)}%` }}>-15m</span>
          <span style={{ left: `${minutesAgo(5)}%` }}>-5m</span>
          <span style={{ left: '100%' }}>now</span>
        </div>
      </div>
    </div>
  );
}
