import { useRef, useEffect, useMemo } from 'react';
import './AdvancedGauges.css';

export function RadialBarChart({ data = [], label = 'DISTRIBUTION', size = 200 }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    const cx = size / 2;
    const cy = size / 2;
    ctx.clearRect(0, 0, size, size);

    const total = data.reduce((sum, d) => sum + d.value, 0) || 1;
    const colors = ['#00ff88', '#00d4ff', '#a855f7', '#ff9f1a', '#ff3b5c', '#00ffcc'];
    
    let startAngle = -Math.PI / 2;
    data.forEach((item, i) => {
      const sliceAngle = (item.value / total) * Math.PI * 2;
      const radius = size * 0.4;
      
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, radius, startAngle, startAngle + sliceAngle);
      ctx.closePath();
      ctx.fillStyle = colors[i % colors.length] + '40';
      ctx.fill();
      ctx.strokeStyle = colors[i % colors.length];
      ctx.lineWidth = 2;
      ctx.stroke();
      
      const midAngle = startAngle + sliceAngle / 2;
      const labelRadius = radius * 0.7;
      const labelX = cx + Math.cos(midAngle) * labelRadius;
      const labelY = cy + Math.sin(midAngle) * labelRadius;
      
      ctx.fillStyle = colors[i % colors.length];
      ctx.font = 'bold 11px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(`${Math.round((item.value / total) * 100)}%`, labelX, labelY);
      
      startAngle += sliceAngle;
    });

    ctx.beginPath();
    ctx.arc(cx, cy, size * 0.25, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(10, 14, 20, 0.9)';
    ctx.fill();

    ctx.fillStyle = 'var(--text-secondary)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(label, cx, cy);
  }, [data, label, size]);

  return <canvas ref={canvasRef} style={{ width: size, height: size }} />;
}

export function NetworkGauge({ upload = 0, download = 0, max = 100, label = 'NETWORK' }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = 200 * dpr;
    const h = 120 * dpr;
    canvas.width = w;
    canvas.height = h;
    ctx.scale(dpr, dpr);

    const cx = 100;
    const cy = 80;
    ctx.clearRect(0, 0, 200, 120);

    // Upload arc (top half)
    ctx.beginPath();
    ctx.arc(cx, cy, 50, Math.PI, 0, false);
    ctx.strokeStyle = 'rgba(0, 255, 136, 0.2)';
    ctx.lineWidth = 14;
    ctx.stroke();

    const uploadAngle = Math.PI - (upload / max) * Math.PI;
    ctx.beginPath();
    ctx.arc(cx, cy, 50, Math.PI, uploadAngle, true);
    ctx.strokeStyle = '#00ff88';
    ctx.lineWidth = 14;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Download arc (bottom half)
    ctx.beginPath();
    ctx.arc(cx, cy, 35, Math.PI, 0, false);
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.2)';
    ctx.lineWidth = 10;
    ctx.stroke();

    const downloadAngle = Math.PI - (download / max) * Math.PI;
    ctx.beginPath();
    ctx.arc(cx, cy, 35, Math.PI, downloadAngle, true);
    ctx.strokeStyle = '#00d4ff';
    ctx.lineWidth = 10;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Labels
    ctx.fillStyle = '#00ff88';
    ctx.font = 'bold 14px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(`↑ ${upload.toFixed(1)}`, cx - 50, 20);

    ctx.fillStyle = '#00d4ff';
    ctx.fillText(`↓ ${download.toFixed(1)}`, cx + 50, 20);

    ctx.fillStyle = 'var(--text-secondary)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillText(label, cx, 115);
  }, [upload, download, max, label]);

  return <canvas ref={canvasRef} style={{ width: 200, height: 120 }} />;
}

export function SevenSegmentDisplay({ value = 0, digits = 5, label = 'COUNTER', color = '#00ff88' }) {
  const displayValue = Math.abs(value).toString().padStart(digits, '0');
  
  return (
    <div className="seven-segment-wrapper">
      <div className="seven-segment-label">{label}</div>
      <div className="seven-segment-display" style={{ color }}>
        {displayValue.split('').map((digit, i) => (
          <div key={i} className="segment-digit">{digit}</div>
        ))}
      </div>
      <div className="seven-segment-unit" style={{ color }}>
        {color === '#ff3b5c' ? 'ERRORS' : color === '#ff9f1a' ? 'WARNINGS' : 'COUNT'}
      </div>
    </div>
  );
}

export function LatencyHistogram({ data = [], label = 'LATENCY DISTRIBUTION' }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = 260 * dpr;
    const h = 100 * dpr;
    canvas.width = w;
    canvas.height = h;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, 260, 100);

    const barWidth = 20;
    const gap = 4;
    const maxVal = Math.max(...data.map(d => d.value), 1);
    const colors = ['#00ff88', '#00d4ff', '#00ffcc', '#a855f7', '#ff9f1a', '#ff3b5c'];

    data.forEach((item, i) => {
      const x = i * (barWidth + gap) + 10;
      const height = (item.value / maxVal) * 70;
      const y = 80 - height;
      
      const grad = ctx.createLinearGradient(x, y, x, 80);
      grad.addColorStop(0, colors[i % colors.length]);
      grad.addColorStop(1, colors[i % colors.length] + '20');
      
      ctx.fillStyle = grad;
      ctx.fillRect(x, y, barWidth, height);
      
      ctx.strokeStyle = colors[i % colors.length] + '60';
      ctx.strokeRect(x, y, barWidth, height);

      ctx.fillStyle = 'var(--text-secondary)';
      ctx.font = '8px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.fillText(item.label, x + barWidth / 2, 95);
    });

    ctx.fillStyle = 'var(--text-secondary)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(label, 10, 12);
  }, [data, label]);

  return <canvas ref={canvasRef} style={{ width: 260, height: 100 }} />;
}

export function CircularProgress({ value = 0, max = 100, size = 80, strokeWidth = 8, color = '#00d4ff', label = '' }) {
  const canvasRef = useRef(null);
  const percent = (value / max) * 100;
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    const cx = size / 2;
    const cy = size / 2;
    const radius = (size - strokeWidth) / 2;

    ctx.clearRect(0, 0, size, size);

    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.1)';
    ctx.lineWidth = strokeWidth;
    ctx.stroke();

    const startAngle = -Math.PI / 2;
    const endAngle = startAngle + (percent / 100) * Math.PI * 2;
    
    ctx.beginPath();
    ctx.arc(cx, cy, radius, startAngle, endAngle);
    ctx.strokeStyle = color;
    ctx.lineWidth = strokeWidth;
    ctx.lineCap = 'round';
    ctx.stroke();

    ctx.fillStyle = color;
    ctx.font = 'bold 16px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(Math.round(percent) + '%', cx, cy);

    if (label) {
      ctx.fillStyle = 'var(--text-secondary)';
      ctx.font = '8px "JetBrains Mono", monospace';
      ctx.fillText(label, cx, cy + 14);
    }
  }, [value, max, size, strokeWidth, color, label, percent]);

  return <canvas ref={canvasRef} style={{ width: size, height: size }} />;
}

export function ThermometerGauge({ value = 0, min = 0, max = 100, label = 'TEMP', warningLevel = 70, criticalLevel = 90 }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = 60 * dpr;
    canvas.height = 160 * dpr;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, 60, 160);

    const bulbRadius = 18;
    const tubeWidth = 12;
    const tubeHeight = 100;
    const tubeX = 30 - tubeWidth / 2;
    const tubeY = 10;

    // Tube background
    ctx.fillStyle = 'rgba(0, 0, 0, 0.4)';
    ctx.beginPath();
    ctx.roundRect(tubeX, tubeY, tubeWidth, tubeHeight, 6);
    ctx.fill();

    // Tick marks
    for (let i = 0; i <= 10; i++) {
      const y = tubeY + tubeHeight - (i / 10) * tubeHeight;
      ctx.strokeStyle = 'rgba(232, 238, 245, 0.3)';
      ctx.lineWidth = i % 5 === 0 ? 1 : 0.5;
      ctx.beginPath();
      ctx.moveTo(tubeX + tubeWidth + 2, y);
      ctx.lineTo(tubeX + tubeWidth + (i % 5 === 0 ? 8 : 5), y);
      ctx.stroke();
    }

    // Fill level
    const fillPercent = Math.min((value - min) / (max - min), 1);
    const fillHeight = fillPercent * (tubeHeight - 4);

    let fillColor = '#00ff88';
    if (value >= criticalLevel) fillColor = '#ff3b5c';
    else if (value >= warningLevel) fillColor = '#ff9f1a';

    const grad = ctx.createLinearGradient(30, tubeY + tubeHeight - fillHeight, 30, tubeY + tubeHeight);
    grad.addColorStop(0, fillColor);
    grad.addColorStop(1, fillColor + '80');

    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.roundRect(tubeX + 2, tubeY + tubeHeight - fillHeight, tubeWidth - 4, fillHeight, 2);
    ctx.fill();

    // Bulb
    ctx.beginPath();
    ctx.arc(30, tubeY + tubeHeight + bulbRadius - 2, bulbRadius, 0, Math.PI * 2);
    ctx.fillStyle = fillColor;
    ctx.fill();

    ctx.beginPath();
    ctx.arc(26, tubeY + tubeHeight + bulbRadius - 8, 4, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255, 255, 255, 0.3)';
    ctx.fill();

    // Value
    ctx.fillStyle = fillColor;
    ctx.font = 'bold 12px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(Math.round(value), 30, tubeY + tubeHeight + bulbRadius + 25);

    // Label
    ctx.fillStyle = 'var(--text-secondary)';
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.fillText(label, 30, 155);
  }, [value, min, max, label, warningLevel, criticalLevel]);

  return <canvas ref={canvasRef} style={{ width: 60, height: 160 }} />;
}

export function WaveformDisplay({ data = [], label = 'SIGNAL', color = '#00d4ff' }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = 280 * dpr;
    const h = 80 * dpr;
    canvas.width = w;
    canvas.height = h;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, 280, 80);

    // Grid
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.1)';
    ctx.lineWidth = 0.5;
    for (let i = 0; i < 8; i++) {
      ctx.beginPath();
      ctx.moveTo(10, 10 + i * 8);
      ctx.lineTo(270, 10 + i * 8);
      ctx.stroke();
    }

    // Waveform
    if (data.length > 1) {
      ctx.beginPath();
      ctx.moveTo(10, 40 - data[0] * 30);
      data.forEach((val, i) => {
        const x = 10 + (i / (data.length - 1)) * 260;
        const y = 40 - val * 30;
        ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();

      // Glow effect
      ctx.shadowColor = color;
      ctx.shadowBlur = 8;
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    ctx.fillStyle = 'var(--text-secondary)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillText(label, 10, 75);
  }, [data, label, color]);

  return <canvas ref={canvasRef} style={{ width: 280, height: 80 }} />;
}

export function FuelGauge({ value = 75, label = 'STORAGE' }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = 120 * dpr;
    canvas.height = 80 * dpr;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, 120, 80);

    const cx = 60;
    const cy = 55;
    const radius = 35;

    // E/F arc
    ctx.beginPath();
    ctx.arc(cx, cy, radius, Math.PI * 1.15, Math.PI * 1.85, false);
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.2)';
    ctx.lineWidth = 8;
    ctx.stroke();

    // Fill arc
    let fillColor = '#00ff88';
    if (value < 25) fillColor = '#ff3b5c';
    else if (value < 50) fillColor = '#ff9f1a';

    const startAngle = Math.PI * 1.15;
    const endAngle = startAngle + (value / 100) * Math.PI * 0.7;
    
    ctx.beginPath();
    ctx.arc(cx, cy, radius, startAngle, endAngle, false);
    ctx.strokeStyle = fillColor;
    ctx.lineWidth = 8;
    ctx.lineCap = 'round';
    ctx.stroke();

    // E/F labels
    ctx.fillStyle = '#ff3b5c';
    ctx.font = 'bold 10px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText('E', 15, 72);
    
    ctx.fillStyle = '#00ff88';
    ctx.textAlign = 'right';
    ctx.fillText('F', 105, 72);

    // Value
    ctx.fillStyle = fillColor;
    ctx.font = 'bold 18px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(Math.round(value) + '%', cx, cy + 5);

    // Needle
    const needleAngle = startAngle + (value / 100) * Math.PI * 0.7;
    const needleLen = radius * 0.6;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(needleAngle) * needleLen, cy + Math.sin(needleAngle) * needleLen);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.fillStyle = 'var(--text-secondary)';
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.fillText(label, cx, 20);
  }, [value, label]);

  return <canvas ref={canvasRef} style={{ width: 120, height: 80 }} />;
}

export function MultiRingGauge({ rings = [], size = 160 }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    const cx = size / 2;
    const cy = size / 2;
    ctx.clearRect(0, 0, size, size);

    rings.forEach((ring, i) => {
      const radius = (size / 2) - 15 - i * 18;
      const strokeWidth = 10;
      const percent = (ring.value / ring.max) * 100;
      
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(0, 0, 0, 0.3)';
      ctx.lineWidth = strokeWidth;
      ctx.stroke();

      const startAngle = -Math.PI / 2;
      const endAngle = startAngle + (percent / 100) * Math.PI * 2;
      
      ctx.beginPath();
      ctx.arc(cx, cy, radius, startAngle, endAngle);
      ctx.strokeStyle = ring.color;
      ctx.lineWidth = strokeWidth;
      ctx.lineCap = 'round';
      ctx.stroke();

      // Label
      ctx.fillStyle = ring.color;
      ctx.font = '8px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      const labelAngle = startAngle + Math.PI * 0.25;
      const labelRadius = radius + 12;
      ctx.fillText(ring.label, cx + Math.cos(labelAngle) * labelRadius, cy + Math.sin(labelAngle) * labelRadius);
    });

    // Center display
    ctx.beginPath();
    ctx.arc(cx, cy, 25, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(10, 14, 20, 0.9)';
    ctx.fill();
  }, [rings, size]);

  return <canvas ref={canvasRef} style={{ width: size, height: size }} />;
}

export function CompassGauge({ value = 0, label = 'HEADING' }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const size = 120;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    const cx = size / 2;
    const cy = size / 2;
    const radius = 50;
    ctx.clearRect(0, 0, size, size);

    // Outer ring
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = '#00d4ff40';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Cardinal directions
    const directions = ['N', 'E', 'S', 'W'];
    directions.forEach((dir, i) => {
      const angle = (i * Math.PI / 2) - Math.PI / 2;
      const x = cx + Math.cos(angle) * (radius - 12);
      const y = cy + Math.sin(angle) * (radius - 12);
      ctx.fillStyle = dir === 'N' ? '#ff3b5c' : '#00d4ff';
      ctx.font = 'bold 10px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(dir, x, y);
    });

    // Needle
    const needleAngle = (value / 360) * Math.PI * 2 - Math.PI / 2;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(needleAngle) * 35, cy + Math.sin(needleAngle) * 35);
    ctx.lineTo(cx + Math.cos(needleAngle + Math.PI) * 15, cy + Math.sin(needleAngle + Math.PI) * 15);
    ctx.strokeStyle = '#ff3b5c';
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Center pivot
    ctx.beginPath();
    ctx.arc(cx, cy, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#00d4ff';
    ctx.fill();

    // Value
    ctx.fillStyle = 'var(--text-secondary)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillText(`${Math.round(value)}°`, cx, 10);
    ctx.fillText(label, cx, size - 5);
  }, [value, label]);

  return <canvas ref={canvasRef} style={{ width: 120, height: 120 }} />;
}

export function Oscilloscope({ channel1 = [], channel2 = [], timebase = '10ms/div' }) {
  const canvasRef = useRef(null);
  
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = 280 * dpr;
    const h = 160 * dpr;
    canvas.width = w;
    canvas.height = h;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, 280, 160);

    // CRT background
    ctx.fillStyle = '#001a00';
    ctx.fillRect(0, 0, 280, 160);

    // Grid overlay
    ctx.strokeStyle = '#003300';
    ctx.lineWidth = 0.5;
    for (let x = 0; x < 280; x += 28) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, 160);
      ctx.stroke();
    }
    for (let y = 0; y < 160; y += 16) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(280, y);
      ctx.stroke();
    }

    // Center lines
    ctx.strokeStyle = '#004400';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, 80);
    ctx.lineTo(280, 80);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(140, 0);
    ctx.lineTo(140, 160);
    ctx.stroke();

    // Channel 1
    if (channel1.length > 1) {
      ctx.beginPath();
      ctx.moveTo(0, 80 - channel1[0] * 60);
      channel1.forEach((val, i) => {
        ctx.lineTo((i / (channel1.length - 1)) * 280, 80 - val * 60);
      });
      ctx.strokeStyle = '#00ff88';
      ctx.lineWidth = 1.5;
      ctx.shadowColor = '#00ff88';
      ctx.shadowBlur = 4;
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    // Channel 2
    if (channel2.length > 1) {
      ctx.beginPath();
      ctx.moveTo(0, 80 - channel2[0] * 60);
      channel2.forEach((val, i) => {
        ctx.lineTo((i / (channel2.length - 1)) * 280, 80 - val * 60);
      });
      ctx.strokeStyle = '#ff9f1a';
      ctx.lineWidth = 1.5;
      ctx.shadowColor = '#ff9f1a';
      ctx.shadowBlur = 4;
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    // Scanline effect
    for (let y = 0; y < 160; y += 2) {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.03)';
      ctx.fillRect(0, y, 280, 1);
    }

    // Info overlay
    ctx.fillStyle = '#00ff88';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillText(`CH1: ${(channel1[channel1.length - 1] * 5).toFixed(1)}V`, 5, 12);
    ctx.fillStyle = '#ff9f1a';
    ctx.fillText(`CH2: ${(channel2[channel2.length - 1] * 5).toFixed(1)}V`, 5, 24);
    ctx.fillStyle = '#00ff88';
    ctx.fillText(timebase, 220, 155);
  }, [channel1, channel2, timebase]);

  return <canvas ref={canvasRef} style={{ width: 280, height: 160, borderRadius: '4px', border: '2px solid #333' }} />;
}
