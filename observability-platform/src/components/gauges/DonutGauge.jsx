import { useRef, useEffect, useState } from 'react';

export default function DonutGauge({
  value = 0,
  label = 'PERCENTAGE',
  size = 120,
  strokeWidth = 12,
  warningThreshold = 70,
  criticalThreshold = 90,
  color = '#00d4ff',
  showGlow = true,
}) {
  const canvasRef = useRef(null);
  const [animValue, setAnimValue] = useState(0);

  useEffect(() => {
    let frame;
    let start = performance.now();
    const duration = 800;
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
    const radius = (size - strokeWidth) / 2;

    ctx.clearRect(0, 0, size, size);

    let activeColor = color;
    if (animValue >= criticalThreshold) activeColor = '#ff3b5c';
    else if (animValue >= warningThreshold) activeColor = '#ff9f1a';

    if (showGlow) {
      const glow = ctx.createRadialGradient(cx, cy, radius * 0.8, cx, cy, radius * 1.2);
      glow.addColorStop(0, 'transparent');
      glow.addColorStop(0.5, activeColor + '15');
      glow.addColorStop(1, 'transparent');
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, size, size);
    }

    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(0, 212, 255, 0.1)';
    ctx.lineWidth = strokeWidth;
    ctx.stroke();

    const startAngle = -Math.PI / 2;
    const percent = Math.min(animValue / 100, 1);
    const endAngle = startAngle + percent * Math.PI * 2;

    if (percent > 0) {
      ctx.beginPath();
      ctx.arc(cx, cy, radius, startAngle, endAngle);
      const grad = ctx.createLinearGradient(0, 0, size, size);
      grad.addColorStop(0, activeColor);
      grad.addColorStop(1, activeColor + 'aa');
      ctx.strokeStyle = grad;
      ctx.lineWidth = strokeWidth;
      ctx.lineCap = 'round';
      ctx.stroke();
    }

    ctx.beginPath();
    ctx.arc(cx, cy, radius - strokeWidth / 2 - 4, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(10, 14, 20, 0.6)';
    ctx.fill();

    ctx.fillStyle = activeColor;
    ctx.font = `bold ${size * 0.22}px "JetBrains Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${Math.round(animValue)}%`, cx, cy - 4);

    ctx.fillStyle = 'rgba(232, 238, 245, 0.4)';
    ctx.font = `${size * 0.08}px "JetBrains Mono", monospace`;
    ctx.fillText(label, cx, cy + size * 0.15);
  }, [animValue, label, size, strokeWidth, warningThreshold, criticalThreshold, color, showGlow]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: size, height: size }}
    />
  );
}
