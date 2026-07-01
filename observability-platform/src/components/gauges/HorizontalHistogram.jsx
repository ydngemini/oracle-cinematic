import { useRef, useEffect, useState } from 'react';

export default function HorizontalHistogram({
  data = [],
  label = 'DISTRIBUTION',
  width = 280,
  height = 100,
  maxBins = 20,
  color = '#00d4ff',
}) {
  const canvasRef = useRef(null);
  const [animProgress, setAnimProgress] = useState(0);

  useEffect(() => {
    let frame;
    const start = performance.now();
    const duration = 800;

    const animate = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      setAnimProgress(1 - Math.pow(1 - progress, 3));
      if (progress < 1) frame = requestAnimationFrame(animate);
    };

    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [data]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, width, height);

    const padding = { top: 20, right: 15, bottom: 25, left: 10 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;

    const bins = data.length > 0 ? data : Array.from({ length: maxBins }, (_, i) => ({
      count: Math.floor(Math.random() * 50),
      range: i,
    }));

    const maxCount = Math.max(...bins.map(b => b.count), 1);
    const barWidth = chartWidth / bins.length;

    bins.forEach((bin, i) => {
      const barHeight = (bin.count / maxCount) * chartHeight * animProgress;
      const x = padding.left + i * barWidth;
      const y = height - padding.bottom - barHeight;

      const intensity = bin.count / maxCount;
      const barColor = intensity > 0.7 ? '#ff3b5c' : intensity > 0.4 ? '#ff9f1a' : color;

      const grad = ctx.createLinearGradient(x, y, x, height - padding.bottom);
      grad.addColorStop(0, barColor);
      grad.addColorStop(1, barColor + '20');
      ctx.fillStyle = grad;
      ctx.fillRect(x + 1, y, barWidth - 2, barHeight);

      ctx.strokeStyle = barColor + '40';
      ctx.lineWidth = 0.5;
      ctx.strokeRect(x + 1, y, barWidth - 2, barHeight);
    });

    ctx.strokeStyle = 'rgba(232, 238, 245, 0.1)';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
      const y = padding.top + (chartHeight / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(width - padding.right, y);
      ctx.stroke();
    }

    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(label, padding.left, 14);

    const avgY = height - padding.bottom - (bins.reduce((s, b) => s + b.count, 0) / bins.length / maxCount) * chartHeight * animProgress;
    ctx.strokeStyle = '#ff9f1a';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padding.left, avgY);
    ctx.lineTo(width - padding.right, avgY);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = '#ff9f1a';
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    ctx.fillText('AVG', width - padding.right, avgY - 4);
  }, [data, label, width, height, maxBins, color, animProgress]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width, height }}
    />
  );
}
