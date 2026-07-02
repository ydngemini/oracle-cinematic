import { useRef, useEffect, useMemo } from 'react';
import './RetroGauges.css';

export function SpeedGauge({ value = 0, max = 100, label = 'THROUGHPUT', unit = 'req/s', color = '#00ff88' }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth * dpr;
    const h = canvas.clientHeight * dpr;
    canvas.width = w;
    canvas.height = h;
    ctx.scale(dpr, dpr);

    const cx = canvas.clientWidth / 2;
    const cy = canvas.clientHeight * 0.55;
    const radius = Math.min(cx, cy) * 0.85;

    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);

    // Outer glow ring
    const glowGrad = ctx.createRadialGradient(cx, cy, radius * 0.8, cx, cy, radius * 1.2);
    glowGrad.addColorStop(0, 'transparent');
    glowGrad.addColorStop(0.5, `${color}22`);
    glowGrad.addColorStop(1, 'transparent');
    ctx.fillStyle = glowGrad;
    ctx.fillRect(0, 0, canvas.clientWidth, canvas.clientHeight);

    // Main arc background
    ctx.beginPath();
    ctx.arc(cx, cy, radius, Math.PI * 0.75, Math.PI * 2.25, false);
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.15)';
    ctx.lineWidth = 8;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Tick marks
    for (let i = 0; i <= 10; i++) {
      const angle = Math.PI * 0.75 + (i / 10) * Math.PI * 1.5;
      const inner = radius * 0.85;
      const outer = radius * (i % 2 === 0 ? 0.98 : 0.92);
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(angle) * inner, cy + Math.sin(angle) * inner);
      ctx.lineTo(cx + Math.cos(angle) * outer, cy + Math.sin(angle) * outer);
      ctx.strokeStyle = i % 2 === 0 ? 'rgba(0, 212, 255, 0.5)' : 'rgba(0, 212, 255, 0.25)';
      ctx.lineWidth = i % 2 === 0 ? 2 : 1;
      ctx.stroke();

      if (i % 2 === 0) {
        const val = Math.round((max / 10) * i);
        const textR = radius * 0.72;
        ctx.fillStyle = 'rgba(232, 238, 245, 0.6)';
        ctx.font = '10px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(val.toString(), cx + Math.cos(angle) * textR, cy + Math.sin(angle) * textR);
      }
    }

    // Value arc
    const percent = Math.min(value / max, 1);
    const startAngle = Math.PI * 0.75;
    const endAngle = startAngle + percent * Math.PI * 1.5;
    
    if (percent > 0) {
      ctx.beginPath();
      ctx.arc(cx, cy, radius, startAngle, endAngle, false);
      const arcGrad = ctx.createLinearGradient(cx - radius, cy, cx + radius, cy);
      arcGrad.addColorStop(0, color);
      arcGrad.addColorStop(1, percent > 0.8 ? '#ff3b5c' : color);
      ctx.strokeStyle = arcGrad;
      ctx.lineWidth = 8;
      ctx.lineCap = 'round';
      ctx.stroke();
    }

    // Needle
    const needleAngle = startAngle + percent * Math.PI * 1.5;
    const needleLen = radius * 0.5;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(needleAngle) * needleLen, cy + Math.sin(needleAngle) * needleLen);
    ctx.strokeStyle = '#ff3b5c';
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Center hub
    ctx.beginPath();
    ctx.arc(cx, cy, 12, 0, Math.PI * 2);
    ctx.fillStyle = '#0a0e14';
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx, cy, 6, 0, Math.PI * 2);
    ctx.fillStyle = '#ff3b5c';
    ctx.fill();

    // Digital readout in the lower gap: a prominent value (clear of the side
    // ticks at ~0.51r and of the shortened needle) with a compact label·unit
    // caption below it — no element overlaps the ticks, needle, or hub.
    ctx.textAlign = 'center';
    ctx.fillStyle = color;
    ctx.font = 'bold 22px "JetBrains Mono", monospace';
    ctx.fillText(Math.round(value).toString(), cx, cy + radius * 0.6);
    ctx.fillStyle = 'rgba(232, 238, 245, 0.45)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillText(`${label} · ${unit}`, cx, cy + radius * 0.82);
  }, [value, max, label, unit, color]);

  return <canvas ref={canvasRef} className="speed-gauge" />;
}

export function HealthGauge({ value = 0, label = 'CPU', color = '#00d4ff', warningThreshold = 70, criticalThreshold = 90 }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth * dpr;
    const h = canvas.clientHeight * dpr;
    canvas.width = w;
    canvas.height = h;
    ctx.scale(dpr, dpr);

    const cx = canvas.clientWidth / 2;
    const cy = canvas.clientHeight / 2;
    const radius = Math.min(cx, cy) * 0.8;

    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);

    // Glow
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * 1.3);
    let glowColor = `${color}10`;
    if (value >= criticalThreshold) glowColor = '#ff3b5c15';
    else if (value >= warningThreshold) glowColor = '#ff9f1a15';
    grad.addColorStop(0, glowColor);
    grad.addColorStop(1, 'transparent');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, canvas.clientWidth, canvas.clientHeight);

    // Background arc
    ctx.beginPath();
    ctx.arc(cx, cy, radius, Math.PI * 1.35, Math.PI * 1.65, false);
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.1)';
    ctx.lineWidth = 12;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Value arc
    const displayValue = Math.min(value, 100);
    const startAngle = Math.PI * 1.35;
    const sweepAngle = (displayValue / 100) * Math.PI * 0.5;
    
    let activeColor = color;
    if (value >= criticalThreshold) activeColor = '#ff3b5c';
    else if (value >= warningThreshold) activeColor = '#ff9f1a';

    ctx.beginPath();
    ctx.arc(cx, cy, radius, startAngle, startAngle + sweepAngle, false);
    const arcGrad = ctx.createLinearGradient(cx - radius, cy, cx + radius, cy);
    arcGrad.addColorStop(0, activeColor);
    ctx.strokeStyle = arcGrad;
    ctx.lineWidth = 12;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Value text
    ctx.fillStyle = activeColor;
    ctx.font = 'bold 22px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${Math.round(displayValue)}%`, cx, cy * 0.85);

    // Label
    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillText(label, cx, cy * 1.35);
  }, [value, label, color, warningThreshold, criticalThreshold]);

  return <canvas ref={canvasRef} className="health-gauge" />;
}

export function DialControl({ value = 50, min = 0, max = 100, label = 'THRESHOLD', onChange }) {
  const canvasRef = useRef(null);
  const dragging = useRef(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = canvas.clientWidth * dpr;
    canvas.height = canvas.clientHeight * dpr;
    ctx.scale(dpr, dpr);

    const cx = canvas.clientWidth / 2;
    const cy = canvas.clientHeight / 2;
    const radius = Math.min(cx, cy) * 0.7;

    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);

    // Outer ring
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.2)';
    ctx.lineWidth = 3;
    ctx.stroke();

    // Groove
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.85, Math.PI * 0.8, Math.PI * 2.2, false);
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.5)';
    ctx.lineWidth = 8;
    ctx.stroke();

    // Value track
    const percent = (value - min) / (max - min);
    const startAngle = Math.PI * 0.8;
    const trackAngle = startAngle + percent * Math.PI * 1.4;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.85, startAngle, trackAngle, false);
    ctx.strokeStyle = '#a855f7';
    ctx.lineWidth = 8;
    ctx.stroke();

    // Knob
    const knobAngle = startAngle + percent * Math.PI * 1.4;
    const knobX = cx + Math.cos(knobAngle) * radius * 0.85;
    const knobY = cy + Math.sin(knobAngle) * radius * 0.85;
    
    ctx.beginPath();
    ctx.arc(knobX, knobY, 10, 0, Math.PI * 2);
    ctx.fillStyle = '#a855f7';
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Center
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.5, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
    ctx.fill();

    // Value
    ctx.fillStyle = '#a855f7';
    ctx.font = 'bold 18px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(Math.round(value).toString(), cx, cy);

    // Label
    ctx.fillStyle = 'rgba(232, 238, 245, 0.4)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillText(label, cx, cy + radius + 12);
  }, [value, min, max, label]);

  const handleMouseDown = (e) => {
    if (!onChange) return;
    dragging.current = true;
    updateValue(e);
  };

  const updateValue = (e) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    const x = e.clientX - rect.left - cx;
    const y = e.clientY - rect.top - cy;
    let angle = Math.atan2(y, x);
    if (angle < Math.PI * 0.8) angle += Math.PI * 2;
    const normalizedAngle = angle - Math.PI * 0.8;
    let percent = normalizedAngle / (Math.PI * 1.4);
    percent = Math.max(0, Math.min(1, percent));
    const newValue = min + percent * (max - min);
    onChange(Math.round(newValue));
  };

  useEffect(() => {
    const handleMove = (e) => {
      if (dragging.current) updateValue(e);
    };
    const handleUp = () => {
      dragging.current = false;
    };
    window.addEventListener('mousemove', handleMove);
    window.addEventListener('mouseup', handleUp);
    return () => {
      window.removeEventListener('mousemove', handleMove);
      window.removeEventListener('mouseup', handleUp);
    };
  }, [onChange]);

  return (
    <canvas 
      ref={canvasRef} 
      className="dial-control" 
      onMouseDown={handleMouseDown}
      style={{ cursor: 'pointer' }}
    />
  );
}

export function IndicatorLight({ status = 'ok', label = 'STATUS' }) {
  const colors = {
    ok: '#00ff88',
    warning: '#ff9f1a',
    critical: '#ff3b5c',
    off: '#333',
  };
  const color = colors[status] || colors.off;
  
  return (
    <div className="indicator-light-wrapper">
      <div 
        className="indicator-light" 
        style={{ 
          boxShadow: `0 0 ${status !== 'off' ? '20px' : '5px'} ${color}, inset 0 0 10px ${color}40`,
          background: `radial-gradient(circle at 30% 30%, ${color}80, ${status !== 'off' ? color : '#222'} 70%)`,
        }} 
      />
      <div className="indicator-label">{label}</div>
    </div>
  );
}

export function MultiGaugeRow({ services = [], metrics = {} }) {
  return (
    <div className="multi-gauge-row">
      {services.map((svc, i) => {
        const m = metrics[svc.id] || {};
        return (
          <div key={svc.id} className="mini-gauge">
            <HealthGauge 
              value={m.cpu?.avg || 0} 
              label={svc.type.toUpperCase()} 
              color={svc.type === 'ec2' ? '#00d4ff' : '#ff9f1a'}
            />
          </div>
        );
      })}
    </div>
  );
}

export function TurboGauge({ rpm = 0, boost = 0, temp = 0 }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = canvas.clientWidth * dpr;
    canvas.height = canvas.clientHeight * dpr;
    ctx.scale(dpr, dpr);

    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const cx = w / 2;
    const cy = h * 0.48;

    ctx.clearRect(0, 0, w, h);

    // TURBO watermark — size it to fit the canvas so it never clips at the edges.
    ctx.fillStyle = 'rgba(0, 212, 255, 0.1)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    let turboSize = Math.round(Math.min(cx, cy) * 0.62);
    ctx.font = `bold ${turboSize}px "Orbitron", sans-serif`;
    while (turboSize > 10 && ctx.measureText('TURBO').width > cx * 1.7) {
      turboSize -= 2;
      ctx.font = `bold ${turboSize}px "Orbitron", sans-serif`;
    }
    ctx.fillText('TURBO', cx, cy);

    // Main gauge arc
    const radius = Math.min(cx, cy) * 0.95;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, Math.PI * 0.7, Math.PI * 2.3, false);
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.1)';
    ctx.lineWidth = 20;
    ctx.stroke();

    // Boost arc
    const boostPercent = Math.min(boost / 20, 1);
    const startAngle = Math.PI * 0.7;
    const sweepAngle = boostPercent * Math.PI * 1.6;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, startAngle, startAngle + sweepAngle, false);
    const boostGrad = ctx.createLinearGradient(cx - radius, cy, cx + radius, cy);
    boostGrad.addColorStop(0, '#00ff88');
    boostGrad.addColorStop(0.6, '#00d4ff');
    boostGrad.addColorStop(1, '#a855f7');
    ctx.strokeStyle = boostGrad;
    ctx.lineWidth = 20;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Boost value
    ctx.fillStyle = '#00d4ff';
    ctx.font = 'bold 36px "JetBrains Mono", monospace';
    ctx.fillText(boost.toFixed(1), cx, cy - 10);
    ctx.fillStyle = 'rgba(232, 238, 245, 0.4)';
    ctx.font = '12px "JetBrains Mono", monospace';
    ctx.fillText('PSI', cx, cy + 25);

    // RPM bar
    const rpmBarY = h * 0.85;
    const rpmBarW = w * 0.7;
    const rpmBarX = (w - rpmBarW) / 2;
    ctx.fillStyle = 'rgba(0, 0, 0, 0.4)';
    ctx.fillRect(rpmBarX, rpmBarY, rpmBarW, 16);
    
    const rpmGrad = ctx.createLinearGradient(rpmBarX, 0, rpmBarX + rpmBarW, 0);
    rpmGrad.addColorStop(0, '#00ff88');
    rpmGrad.addColorStop(0.7, '#ff9f1a');
    rpmGrad.addColorStop(1, '#ff3b5c');
    ctx.fillStyle = rpmGrad;
    ctx.fillRect(rpmBarX, rpmBarY, (rpm / 8000) * rpmBarW, 16);

    ctx.fillStyle = '#0a0e14';
    ctx.font = 'bold 10px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(Math.round(rpm).toString(), rpmBarX + 8, rpmBarY + 11);
    ctx.textAlign = 'right';
    ctx.fillText('RPM', rpmBarX + rpmBarW - 8, rpmBarY + 11);

    // Temp indicator
    const tempColor = temp > 85 ? '#ff3b5c' : temp > 70 ? '#ff9f1a' : '#00ff88';
    ctx.beginPath();
    ctx.arc(w * 0.9, h * 0.15, 20, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
    ctx.fill();
    ctx.strokeStyle = tempColor;
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = tempColor;
    ctx.font = 'bold 10px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(`${Math.round(temp)}°`, w * 0.9, h * 0.18);
  }, [rpm, boost, temp]);

  return <canvas ref={canvasRef} className="turbo-gauge" />;
}
