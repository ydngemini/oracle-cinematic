import { useRef, useEffect, useState } from 'react';

export default function StackedAreaChart({
  series = [],
  timeLabels = [],
  width = 320,
  height = 160,
  maxValue = 100,
  label = 'TIME SERIES',
}) {
  const canvasRef = useRef(null);
  const [animProgress, setAnimProgress] = useState(0);

  useEffect(() => {
    let frame;
    const start = performance.now();
    const duration = 1000;

    const animate = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      setAnimProgress(progress);
      if (progress < 1) frame = requestAnimationFrame(animate);
    };

    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [series]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, width, height);

    const padding = { top: 25, right: 15, bottom: 30, left: 35 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;

    const colors = ['#00ff88', '#00d4ff', '#a855f7', '#ff9f1a'];
    const pointCount = series[0]?.data?.length || 0;

    ctx.strokeStyle = 'rgba(232, 238, 245, 0.08)';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
      const y = padding.top + (chartHeight / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(width - padding.right, y);
      ctx.stroke();

      ctx.fillStyle = 'rgba(232, 238, 245, 0.3)';
      ctx.font = '8px "JetBrains Mono", monospace';
      ctx.textAlign = 'right';
      ctx.fillText(Math.round(maxValue - (maxValue / 4) * i), padding.left - 5, y + 3);
    }

    const allData = series.flatMap(s => s.data);
    const maxDataVal = Math.max(...allData, 1);

    series.forEach((s, seriesIndex) => {
      if (!s.data || s.data.length < 2) return;

      const color = colors[seriesIndex % colors.length];
      const pointLen = Math.floor(s.data.length * animProgress);

      if (pointLen < 2) return;

      const points = s.data.slice(0, pointLen).map((val, i) => ({
        x: padding.left + (i / (pointCount - 1)) * chartWidth,
        y: padding.top + chartHeight - (val / maxDataVal) * chartHeight,
      }));

      ctx.beginPath();
      ctx.moveTo(points[0].x, padding.top + chartHeight);
      points.forEach(p => ctx.lineTo(p.x, p.y));
      ctx.lineTo(points[points.length - 1].x, padding.top + chartHeight);
      ctx.closePath();

      const grad = ctx.createLinearGradient(0, padding.top, 0, height - padding.bottom);
      grad.addColorStop(0, color + '40');
      grad.addColorStop(1, color + '05');
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      points.forEach((p, i) => {
        if (i === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();

      const lastPoint = points[points.length - 1];
      ctx.beginPath();
      ctx.arc(lastPoint.x, lastPoint.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    });

    timeLabels.forEach((t, i) => {
      if (i % Math.ceil(timeLabels.length / 5) === 0) {
        const x = padding.left + (i / (timeLabels.length - 1)) * chartWidth;
        ctx.fillStyle = 'rgba(232, 238, 245, 0.3)';
        ctx.font = '8px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillText(t, x, height - padding.bottom + 15);
      }
    });

    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(label, padding.left, 15);

    const legendX = width - padding.right - series.reduce((sum, _, i) => sum + 60, 0);
    series.forEach((s, i) => {
      const x = legendX + i * 60;
      ctx.fillStyle = colors[i % colors.length];
      ctx.fillRect(x, 8, 10, 10);
      ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
      ctx.font = '8px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText(s.name || `S${i}`, x + 14, 16);
    });
  }, [series, timeLabels, width, height, maxValue, label, animProgress]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width, height }}
    />
  );
}
