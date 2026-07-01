import { useRef, useEffect, useState } from 'react';

export default function WaterfallGauge({
  p50 = 0,
  p95 = 0,
  p99 = 0,
  label = 'LATENCY',
  maxLatency = 1000,
  width = 280,
  height = 140,
  warningThreshold = 500,
  criticalThreshold = 800,
}) {
  const canvasRef = useRef(null);
  const [animP50, setAnimP50] = useState(0);
  const [animP95, setAnimP95] = useState(0);
  const [animP99, setAnimP99] = useState(0);

  useEffect(() => {
    let frame;
    const start = performance.now();
    const duration = 800;
    const startP50 = animP50;
    const startP95 = animP95;
    const startP99 = animP99;

    const animate = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setAnimP50(startP50 + (p50 - startP50) * eased);
      setAnimP95(startP95 + (p95 - startP95) * eased);
      setAnimP99(startP99 + (p99 - startP99) * eased);
      if (progress < 1) frame = requestAnimationFrame(animate);
    };

    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [p50, p95, p99]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, width, height);

    const barWidth = 60;
    const gap = 20;
    const maxBarHeight = height - 50;
    const startX = (width - 3 * barWidth - 2 * gap) / 2;

    const getColor = (val) => {
      if (val >= criticalThreshold) return '#ff3b5c';
      if (val >= warningThreshold) return '#ff9f1a';
      return '#00ff88';
    };

    const metrics = [
      { label: 'P50', value: animP50, color: getColor(animP50) },
      { label: 'P95', value: animP95, color: getColor(animP95) },
      { label: 'P99', value: animP99, color: getColor(animP99) },
    ];

    metrics.forEach((metric, i) => {
      const x = startX + i * (barWidth + gap);
      const barHeight = (metric.value / maxLatency) * maxBarHeight;
      const y = height - 35 - barHeight;

      ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
      ctx.beginPath();
      ctx.roundRect(x, height - 35 - maxBarHeight, barWidth, maxBarHeight, 4);
      ctx.fill();

      const grad = ctx.createLinearGradient(x, y, x, height - 35);
      grad.addColorStop(0, metric.color);
      grad.addColorStop(1, metric.color + '40');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.roundRect(x, y, barWidth, barHeight, 4);
      ctx.fill();

      ctx.strokeStyle = metric.color + '60';
      ctx.lineWidth = 1;
      ctx.stroke();

      ctx.fillStyle = metric.color;
      ctx.font = 'bold 14px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.fillText(Math.round(metric.value), x + barWidth / 2, y - 8);

      ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
      ctx.font = '9px "JetBrains Mono", monospace';
      ctx.fillText(metric.label, x + barWidth / 2, height - 18);
    });

    ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
    ctx.lineWidth = 0.5;
    ctx.setLineDash([4, 4]);
    [0.25, 0.5, 0.75].forEach((pct) => {
      const y = height - 35 - maxBarHeight * pct;
      ctx.beginPath();
      ctx.moveTo(startX - 10, y);
      ctx.lineTo(startX + 3 * barWidth + 2 * gap + 10, y);
      ctx.stroke();
    });
    ctx.setLineDash([]);

    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(label, 10, 16);

    ctx.textAlign = 'right';
    ctx.fillText('ms', width - 10, 16);
  }, [animP50, animP95, animP99, label, width, height, maxLatency, warningThreshold, criticalThreshold]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width, height }}
    />
  );
}
