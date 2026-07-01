import { useRef, useEffect, useState } from 'react';

export default function RadarTechGauge({
  data = [],
  label = 'METRICS',
  size = 180,
  color = '#00d4ff',
}) {
  const canvasRef = useRef(null);
  const [animData, setAnimData] = useState(data.map(d => ({ ...d, animValue: 0 })));

  useEffect(() => {
    const targetData = data.map(d => ({ ...d, animValue: d.value }));
    const startData = animData.map((d, i) => ({
      ...data[i],
      animValue: d.animValue || 0,
    }));
    
    let frame;
    const start = performance.now();
    const duration = 700;

    const animate = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      
      setAnimData(
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
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    const cx = size / 2;
    const cy = size / 2;
    const radius = size * 0.38;

    ctx.clearRect(0, 0, size, size);

    const rings = [0.25, 0.5, 0.75, 1];
    rings.forEach((ring) => {
      ctx.beginPath();
      for (let i = 0; i <= 6; i++) {
        const angle = (i * Math.PI * 2) / 6 - Math.PI / 2;
        const x = cx + Math.cos(angle) * radius * ring;
        const y = cy + Math.sin(angle) * radius * ring;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle = `rgba(0, 212, 255, ${ring === 1 ? 0.2 : 0.08})`;
      ctx.lineWidth = 0.5;
      ctx.stroke();
    });

    for (let i = 0; i < 6; i++) {
      const angle = (i * Math.PI * 2) / 6 - Math.PI / 2;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius);
      ctx.strokeStyle = 'rgba(0, 212, 255, 0.15)';
      ctx.lineWidth = 0.5;
      ctx.stroke();
    }

    if (animData.length >= 3) {
      ctx.beginPath();
      animData.slice(0, 6).forEach((item, i) => {
        const angle = (i * Math.PI * 2) / 6 - Math.PI / 2;
        const r = (Math.min(item.animValue, 100) / 100) * radius;
        const x = cx + Math.cos(angle) * r;
        const y = cy + Math.sin(angle) * r;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.fillStyle = color + '25';
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();

      animData.slice(0, 6).forEach((item, i) => {
        const angle = (i * Math.PI * 2) / 6 - Math.PI / 2;
        const r = (Math.min(item.animValue, 100) / 100) * radius;
        const x = cx + Math.cos(angle) * r;
        const y = cy + Math.sin(angle) * r;
        
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.strokeStyle = '#0a0e14';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      });
    }

    for (let i = 0; i < 6; i++) {
      const angle = (i * Math.PI * 2) / 6 - Math.PI / 2;
      const labelR = radius + 18;
      const x = cx + Math.cos(angle) * labelR;
      const y = cy + Math.sin(angle) * labelR;
      
      ctx.fillStyle = 'rgba(232, 238, 245, 0.6)';
      ctx.font = '9px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(animData[i]?.label || `A${i + 1}`, x, y);
    }

    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(label, cx, size - 8);
  }, [animData, label, size, color]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: size, height: size }}
    />
  );
}
