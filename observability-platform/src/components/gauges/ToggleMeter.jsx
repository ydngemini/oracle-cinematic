import { useRef, useEffect, useState } from 'react';

export default function ToggleMeter({
  isOn = false,
  label = 'POWER',
  onToggle,
  size = 80,
  onColor = '#00ff88',
  offColor = '#666',
}) {
  const canvasRef = useRef(null);
  const [animProgress, setAnimProgress] = useState(isOn ? 1 : 0);

  useEffect(() => {
    let frame;
    const start = performance.now();
    const duration = 300;
    const startProgress = animProgress;
    const targetProgress = isOn ? 1 : 0;

    const animate = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setAnimProgress(startProgress + (targetProgress - startProgress) * eased);
      if (progress < 1) frame = requestAnimationFrame(animate);
    };

    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [isOn]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = (size * 0.6) * dpr;
    ctx.scale(dpr, dpr);

    const w = size;
    const h = size * 0.6;

    ctx.clearRect(0, 0, w, h);

    const trackWidth = w * 0.7;
    const trackHeight = h * 0.35;
    const trackX = (w - trackWidth) / 2;
    const trackY = (h - trackHeight) / 2;
    const knobRadius = trackHeight * 0.7;

    const color = isOn ? onColor : offColor;

    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
    ctx.beginPath();
    ctx.roundRect(trackX, trackY, trackWidth, trackHeight, trackHeight / 2);
    ctx.fill();

    const fillGrad = ctx.createLinearGradient(trackX, 0, trackX + trackWidth, 0);
    fillGrad.addColorStop(0, color + '20');
    fillGrad.addColorStop(animProgress, color + '60');
    fillGrad.addColorStop(1, 'transparent');
    ctx.fillStyle = fillGrad;
    ctx.beginPath();
    ctx.roundRect(trackX, trackY, trackWidth * animProgress, trackHeight, trackHeight / 2);
    ctx.fill();

    ctx.strokeStyle = color + '40';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(trackX, trackY, trackWidth, trackHeight, trackHeight / 2);
    ctx.stroke();

    const knobX = trackX + knobRadius + (trackWidth - knobRadius * 2) * animProgress;
    const knobY = trackY + trackHeight / 2;

    if (isOn) {
      const glowGrad = ctx.createRadialGradient(knobX, knobY, 0, knobX, knobY, knobRadius * 2);
      glowGrad.addColorStop(0, color + '40');
      glowGrad.addColorStop(1, 'transparent');
      ctx.fillStyle = glowGrad;
      ctx.beginPath();
      ctx.arc(knobX, knobY, knobRadius * 2, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.beginPath();
    ctx.arc(knobX, knobY, knobRadius, 0, Math.PI * 2);
    const knobGrad = ctx.createRadialGradient(knobX - knobRadius * 0.3, knobY - knobRadius * 0.3, 0, knobX, knobY, knobRadius);
    knobGrad.addColorStop(0, '#fff');
    knobGrad.addColorStop(0.5, color);
    knobGrad.addColorStop(1, color + 'aa');
    ctx.fillStyle = knobGrad;
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.fillStyle = isOn ? onColor : 'rgba(232, 238, 245, 0.5)';
    ctx.font = `bold ${size * 0.12}px "JetBrains Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.fillText(label, w / 2, h + 10);

    const statusX = trackX + trackWidth + 10;
    const statusY = trackY + trackHeight / 2;

    ctx.beginPath();
    ctx.arc(statusX, statusY, 5, 0, Math.PI * 2);
    ctx.fillStyle = isOn ? onColor : '#444';
    ctx.fill();

    if (isOn) {
      ctx.strokeStyle = onColor + '60';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(statusX, statusY, 8, 0, Math.PI * 2);
      ctx.stroke();
    }
  }, [animProgress, label, size, onColor, offColor, isOn]);

  const handleClick = () => {
    if (onToggle) onToggle(!isOn);
  };

  return (
    <canvas
      ref={canvasRef}
      style={{ width: size, height: size * 0.75, cursor: 'pointer' }}
      onClick={handleClick}
    />
  );
}
