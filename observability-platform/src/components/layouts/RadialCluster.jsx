import { useRef, useEffect, useMemo } from 'react';
import styles from './RadialCluster.module.css';

export default function RadialCluster({ ec2, rds, metrics, onNodeClick }) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const nodesRef = useRef([]);
  const hoveredNodeRef = useRef(null);

  const services = useMemo(() => {
    const ec2Services = (ec2?.instances || []).map(i => ({
      id: i.instance_id,
      type: 'ec2',
      state: i.state,
      cpu: metrics?.ec2?.[i.instance_id]?.cpu?.avg || 0,
    }));
    const rdsServices = (rds?.instances || []).map(i => ({
      id: i.db_instance_identifier,
      type: 'rds',
      state: i.status,
      cpu: metrics?.rds?.[i.db_instance_identifier]?.cpu?.avg || 0,
    }));
    return [...ec2Services, ...rdsServices];
  }, [ec2, rds, metrics]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = container.getBoundingClientRect();
    
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;
    const cx = w / 2;
    const cy = h / 2;
    const centerX = cx;
    const centerY = cy;

    ctx.clearRect(0, 0, w, h);

    const ringConfigs = [
      { radius: Math.min(w, h) * 0.25, label: 'CORE', color: '#00ff88' },
      { radius: Math.min(w, h) * 0.4, label: 'SERVICES', color: '#00d4ff' },
      { radius: Math.min(w, h) * 0.55, label: 'RESOURCES', color: '#a855f7' },
    ];

    ringConfigs.forEach((ring, idx) => {
      ctx.beginPath();
      ctx.arc(centerX, centerY, ring.radius, 0, Math.PI * 2);
      ctx.strokeStyle = `${ring.color}20`;
      ctx.lineWidth = 2;
      ctx.setLineDash([4, 8]);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.beginPath();
      ctx.arc(centerX, centerY, ring.radius + 15, 0, Math.PI * 2);
      ctx.strokeStyle = `${ring.color}15`;
      ctx.lineWidth = 30;
      ctx.stroke();

      const labelAngle = -Math.PI / 2;
      const labelX = centerX + Math.cos(labelAngle) * ring.radius;
      const labelY = centerY + Math.sin(labelAngle) * ring.radius - 20;
      ctx.fillStyle = `${ring.color}60`;
      ctx.font = '9px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.fillText(ring.label, labelX, labelY);
    });

    ctx.beginPath();
    ctx.arc(centerX, centerY, 30, 0, Math.PI * 2);
    const coreGrad = ctx.createRadialGradient(centerX, centerY, 0, centerX, centerY, 50);
    coreGrad.addColorStop(0, '#00ff8840');
    coreGrad.addColorStop(1, 'transparent');
    ctx.fillStyle = coreGrad;
    ctx.fill();
    
    ctx.beginPath();
    ctx.arc(centerX, centerY, 20, 0, Math.PI * 2);
    ctx.fillStyle = '#0a0e14';
    ctx.fill();
    ctx.strokeStyle = '#00ff88';
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.fillStyle = '#00ff88';
    ctx.font = 'bold 10px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('ORACLE', centerX, centerY);

    nodesRef.current = [];
    
    services.forEach((svc, idx) => {
      const ringIndex = svc.type === 'rds' ? 0 : 1;
      const ring = ringConfigs[ringIndex];
      const angle = (idx / services.length) * Math.PI * 2 - Math.PI / 2;
      
      const x = centerX + Math.cos(angle) * ring.radius;
      const y = centerY + Math.sin(angle) * ring.radius;
      
      const isHealthy = svc.state === 'running' || svc.state === 'available';
      const baseColor = isHealthy ? (svc.type === 'rds' ? '#ff9f1a' : '#00d4ff') : '#ff3b5c';
      
      nodesRef.current.push({
        x, y, radius: 16,
        id: svc.id,
        type: svc.type,
        state: svc.state,
        cpu: svc.cpu,
      });

      const glowGrad = ctx.createRadialGradient(x, y, 0, x, y, 30);
      glowGrad.addColorStop(0, `${baseColor}30`);
      glowGrad.addColorStop(1, 'transparent');
      ctx.fillStyle = glowGrad;
      ctx.beginPath();
      ctx.arc(x, y, 30, 0, Math.PI * 2);
      ctx.fill();

      ctx.beginPath();
      ctx.arc(x, y, 16, 0, Math.PI * 2);
      ctx.fillStyle = '#0a0e14';
      ctx.fill();
      ctx.strokeStyle = baseColor;
      ctx.lineWidth = 2;
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(x, y, 12, 0, Math.PI * 2);
      ctx.fillStyle = `${baseColor}40`;
      ctx.fill();

      const icon = svc.type === 'rds' ? 'DB' : 'E2';
      ctx.fillStyle = baseColor;
      ctx.font = 'bold 7px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(icon, x, y);

      const orbitRadius = ring.radius + 35;
      const orbitX = centerX + Math.cos(angle) * orbitRadius;
      const orbitY = centerY + Math.sin(angle) * orbitRadius;
      
      ctx.beginPath();
      ctx.arc(orbitX, orbitY, 4, 0, Math.PI * 2);
      ctx.fillStyle = `${baseColor}60`;
      ctx.fill();

      if (svc.cpu > 80) {
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(centerX, centerY);
        ctx.strokeStyle = '#ff3b5c30';
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    });

    const time = Date.now() / 1000;
    const pulseRadius = 40 + Math.sin(time * 2) * 5;
    ctx.beginPath();
    ctx.arc(centerX, centerY, pulseRadius, 0, Math.PI * 2);
    ctx.strokeStyle = '#00ff8820';
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.fillStyle = 'rgba(0, 212, 255, 0.4)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(`${services.length} RESOURCES`, centerX, h - 20);

  }, [services]);

  const handleMouseMove = (e) => {
    const rect = canvasRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    let hoveredNode = null;
    for (const node of nodesRef.current) {
      const dist = Math.sqrt((x - node.x) ** 2 + (y - node.y) ** 2);
      if (dist < node.radius + 5) {
        hoveredNode = node;
        break;
      }
    }

    if (hoveredNode !== hoveredNodeRef.current) {
      hoveredNodeRef.current = hoveredNode;
      canvasRef.current.style.cursor = hoveredNode ? 'pointer' : 'default';
    }
  };

  const handleClick = (e) => {
    if (hoveredNodeRef.current) {
      onNodeClick?.(hoveredNodeRef.current);
    }
  };

  return (
    <div className={styles.radialCluster} ref={containerRef}>
      <div className={styles.clusterHeader}>
        <h3>RADIAL VIEW</h3>
        <div className={styles.stats}>
          <span className={styles.statItem}>
            <span className={styles.statDot} style={{ background: '#00d4ff' }} />
            EC2: {services.filter(s => s.type === 'ec2').length}
          </span>
          <span className={styles.statItem}>
            <span className={styles.statDot} style={{ background: '#ff9f1a' }} />
            RDS: {services.filter(s => s.type === 'rds').length}
          </span>
        </div>
      </div>
      <div className={styles.canvasContainer}>
        <canvas 
          ref={canvasRef} 
          className={styles.clusterCanvas}
          onMouseMove={handleMouseMove}
          onClick={handleClick}
        />
      </div>
    </div>
  );
}
