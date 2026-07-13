import { useState, useEffect, useMemo } from 'react';
import { SpeedGauge, HealthGauge, DialControl, IndicatorLight, TurboGauge } from './RetroGauges.jsx';
import './RetroDashboard.css';

export default function RetroDashboard({ ec2, rds, metrics, billing }) {
  const [alertThreshold, setAlertThreshold] = useState(75);
  const [throughputValue, setThroughputValue] = useState(0);
  const [boostValue, setBoostValue] = useState(0);

  const instances = useMemo(() => [
    ...ec2.instances.map(i => ({ ...i, type: 'ec2' })),
    ...rds.instances.map(i => ({ ...i, type: 'rds', state: i.status === 'available' ? 'running' : 'pending' })),
  ], [ec2, rds]);

  const runningCount = instances.filter(i => i.state === 'running' || i.state === 'available').length;
  
  const avgCpu = useMemo(() => {
    const cpuValues = Object.values(metrics?.ec2 || {})
      .concat(Object.values(metrics?.rds || {}))
      .map(m => m?.cpu?.avg || 0)
      .filter(v => v > 0);
    return cpuValues.length > 0 ? cpuValues.reduce((a, b) => a + b, 0) / cpuValues.length : 0;
  }, [metrics]);

  const avgMemory = useMemo(() => {
    const memValues = Object.values(metrics?.rds || {})
      .map(m => m?.memory?.avg || 0)
      .filter(v => v > 0);
    return memValues.length > 0 ? memValues.reduce((a, b) => a + b, 0) / memValues.length : 0;
  }, [metrics]);

  const statusColor = avgCpu > 90 ? 'critical' : avgCpu > alertThreshold ? 'warning' : 'ok';

  const requestRate = instances.length * 2 + avgCpu / 10;
  const rpmValue = (requestRate / 100) * 8000;

  useEffect(() => {
    const interval = setInterval(() => {
      setThroughputValue(prev => {
        const target = requestRate;
        return prev + (target - prev) * 0.1;
      });
      setBoostValue(prev => {
        const target = avgCpu / 5;
        return prev + (target - prev) * 0.15;
      });
    }, 50);
    return () => clearInterval(interval);
  }, [requestRate, avgCpu]);

  return (
    <div className="retro-dashboard panel">
      <div className="panel-header">
        <h2>Live Dashboard</h2>
        <div className="status-light-strip">
          <IndicatorLight status={statusColor} label="HEALTH" />
          <IndicatorLight status={runningCount > 0 ? 'ok' : 'off'} label="ACTIVE" />
          <IndicatorLight status={billing?.month_total > 100 ? 'warning' : 'ok'} label="COST" />
          <IndicatorLight status="ok" label="SYNC" />
        </div>
      </div>

      <div className="gauge-cluster">
        <div className="gauge-standalone">
          <TurboGauge rpm={rpmValue} boost={boostValue} temp={avgCpu} />
          <div className="gauge-title">INFRASTRUCTURE LOAD</div>
        </div>
      </div>

      <div className="gauge-cluster">
        <div className="gauge-standalone">
          <SpeedGauge 
            value={throughputValue} 
            max={150} 
            label="REQUESTS" 
            unit="req/s"
            color={throughputValue > 100 ? '#ff3b5c' : '#00ff88'}
          />
        </div>
        
        <div className="gauge-standalone">
          <HealthGauge 
            value={avgCpu} 
            label="AVG CPU" 
            color="#00d4ff"
            warningThreshold={alertThreshold}
            criticalThreshold={90}
          />
        </div>

        <div className="gauge-standalone">
          <HealthGauge 
            value={avgMemory} 
            label=" MEMORY" 
            color="#a855f7"
            warningThreshold={80}
            criticalThreshold={95}
          />
        </div>
      </div>

      <div className="dial-row">
        <div className="gauge-standalone">
          <DialControl 
            value={alertThreshold} 
            min={50} 
            max={95} 
            label="ALERT %"
            onChange={setAlertThreshold}
          />
        </div>
        <div className="gauge-standalone">
          <HealthGauge 
            value={billing?.month_total || 0} 
            label="COST $" 
            color="#ff9f1a"
            warningThreshold={100}
            criticalThreshold={500}
          />
        </div>
        <div className="gauge-standalone">
          <HealthGauge 
            value={runningCount} 
            label="INSTANCES" 
            color="#00ff88"
            warningThreshold={instances.length * 0.7}
            criticalThreshold={instances.length}
          />
        </div>
      </div>

      <div className="rpm-bar-container">
        <div 
          className={`rpm-bar ${avgCpu > 90 ? 'critical' : avgCpu > alertThreshold ? 'warning' : 'safe'}`}
          style={{ width: `${avgCpu}%` }}
        />
      </div>
      <div className="rpm-labels">
        <span>0%</span>
        <span>SAFE</span>
        <span>LOAD</span>
        <span>MAX</span>
      </div>
    </div>
  );
}
