import { useRef, useEffect, useState } from 'react';

export default function BarChartGauge({
  data = [],
  label = 'COMPARISON',
  maxValue = 100,
  warningThreshold = 70,
  criticalThreshold = 90,
  height = 200,
  width = 300,
}) {
  const canvasRef = useRef(null);
  const [animatedData, setAnimatedData] = useState(data.map(d => ({ ...d, animValue: 0 })));

  useEffect(() => {
    const targetData = data.map(d => ({ ...d, animValue: d.value }));
    const startData = animatedData.map((d, i) => ({
      ...data[i],
      animValue: d.animValue || 0,
    }));
    
    let frame;
    let progress = 0;
    const duration = 600;
    const start = performance.now();

    const animate = (now) => {
      progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      
      setAnimatedData(
        startData.map((d, i) => ({
          ...d,
          animValue: startData[i].animValue + (targetData[i]?.value || 0 - startData[i].animValue) * eased,
        }))
      );
      
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

    const padding = { top: 30, right: 20, bottom: 40, left: 10 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;
    const barHeight = Math.min(30, (chartHeight - (animatedData.length - 1) * 8) / animatedData.length);
    const gap = 8;

    animatedData.forEach((item, i) => {
      const y = padding.top + i * (barHeight + gap);
      const barWidth = (item.animValue / maxValue) * chartWidth;

      let color = '#00ff88';
      if (item.animValue >= criticalThreshold) color = '#ff3b5c';
      else if (item.animValue >= warningThreshold) color = '#ff9f1a';

      ctx.fillStyle = 'rgba(0, 0, 0, 0.4)';
      ctx.beginPath();
      ctx.roundRect(padding.left, y, chartWidth, barHeight, 4);
      ctx.fill();

      const grad = ctx.createLinearGradient(padding.left, 0, padding.left + barWidth, 0);
      grad.addColorStop(0, color + '60');
      grad.addColorStop(1, color);
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.roundRect(padding.left, y, Math.max(barWidth, 0), barHeight, 4);
      ctx.fill();

      ctx.fillStyle = 'rgba(232, 238, 245, 0.7)';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(item.label || `Item ${i + 1}`, padding.left + 8, y + barHeight / 2);

      ctx.fillStyle = color;
      ctx.textAlign = 'right';
      ctx.fillText(Math.round(item.animValue).toString(), padding.left + Math.min(barWidth, chartWidth) - 8, y + barHeight / 2);
    });

    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(label, padding.left, 15);
  }, [animatedData, label, maxValue, warningThreshold, criticalThreshold, width, height]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width, height }}
    />
  );
}
