import { useRef, useEffect, useState } from 'react';

export default function PulseOximeterGauge({
  value = 100,
  label = 'CONNECTION',
  size = 100,
  isConnected = true,
  pulseRate = 1.5,
  warningThreshold = 50,
  criticalThreshold = 25,
}) {
  const canvasRef = useRef(null);
  const [pulse, setPulse] = useState(0);
  const [animValue, setAnimValue] = useState(value);

  useEffect(() => {
    let frame;
    const start = performance.now();

    const animate = (now) => {
      const elapsed = (now - start) / 1000;
      setPulse((Math.sin(elapsed * pulseRate * Math.PI * 2) + 1) / 2);
      
      if (isConnected) {
        frame = requestAnimationFrame(animate);
      }
    };

    if (isConnected) {
      frame = requestAnimationFrame(animate);
    }
    return () => cancelAnimationFrame(frame);
  }, [isConnected, pulseRate]);

  useEffect(() => {
    let frame;
    const start = performance.now();
    const duration = 600;
    const startValue = animValue;

    const animate = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setAnimValue(startValue + (value - startValue) * eased);
      if (progress < 1) frame = requestAnimationFrame(animate);
    };

    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [value]);

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
    const radius = size * 0.38;

    ctx.clearRect(0, 0, size, size);

    let statusColor = '#00ff88';
    if (!isConnected) statusColor = '#666';
    else if (animValue <= criticalThreshold) statusColor = '#ff3b5c';
    else if (animValue <= warningThreshold) statusColor = '#ff9f1a';

    const pulseRadius = radius + pulse * 15;
    
    if (isConnected) {
      const grad = ctx.createRadialGradient(cx, cy, radius, cx, cy, pulseRadius + 10);
      grad.addColorStop(0, statusColor + '30');
      grad.addColorStop(0.5, statusColor + '10');
      grad.addColorStop(1, 'transparent');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(cx, cy, pulseRadius + 10, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.4)';
    ctx.lineWidth = 3;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    const fillGrad = ctx.createRadialGradient(cx - radius * 0.3, cy - radius * 0.3, 0, cx, cy, radius);
    fillGrad.addColorStop(0, statusColor + '80');
    fillGrad.addColorStop(1, statusColor + '30');
    ctx.fillStyle = fillGrad;
    ctx.fill();

    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = statusColor;
    ctx.lineWidth = isConnected ? 2 + pulse * 2 : 2;
    ctx.stroke();

    ctx.fillStyle = isConnected ? statusColor : '#999';
    ctx.font = `bold ${size * 0.2}px "JetBrains Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${Math.round(animValue)}%`, cx, cy - 4);

    const statusText = isConnected ? 'ONLINE' : 'OFFLINE';
    ctx.fillStyle = 'rgba(232, 238, 245, 0.4)';
    ctx.font = `${size * 0.07}px "JetBrains Mono", monospace`;
    ctx.fillText(statusText, cx, cy + size * 0.12);

    if (isConnected) {
      ctx.beginPath();
      ctx.arc(cx + radius + 8, cy - radius + 5, 4, 0, Math.PI * 2);
      ctx.fillStyle = statusColor;
      ctx.globalAlpha = 0.5 + pulse * 0.5;
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = `${size * 0.08}px "JetBrains Mono", monospace`;
    ctx.fillText(label, cx, size - 8);
  }, [pulse, animValue, label, size, isConnected, warningThreshold, criticalThreshold]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: size, height: size }}
    />
  );
}
