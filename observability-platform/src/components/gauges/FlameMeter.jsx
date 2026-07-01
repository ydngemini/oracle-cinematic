import { useRef, useEffect, useState } from 'react';

export default function FlameMeter({
  errorRate = 0,
  label = 'ERROR RATE',
  width = 120,
  height = 180,
  warningThreshold = 1,
  criticalThreshold = 5,
}) {
  const canvasRef = useRef(null);
  const [animValue, setAnimValue] = useState(0);
  const [flamePhase, setFlamePhase] = useState(0);

  useEffect(() => {
    let frame;
    const start = performance.now();
    const duration = 600;
    const startValue = animValue;

    const animate = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setAnimValue(startValue + (errorRate - startValue) * eased);
      if (progress < 1) frame = requestAnimationFrame(animate);
    };

    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [errorRate]);

  useEffect(() => {
    let frame;
    const start = performance.now();

    const animate = (now) => {
      const phase = ((now - start) / 1000) % (Math.PI * 2);
      setFlamePhase(phase);
      frame = requestAnimationFrame(animate);
    };

    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, width, height);

    const flameHeight = height * 0.55;
    const flameWidth = width * 0.5;
    const flameX = width / 2;
    const flameY = height - 50;

    let intensity = animValue / 10;
    intensity = Math.min(intensity, 1);

    let flameColor = '#ff3b5c';
    if (animValue < warningThreshold) flameColor = '#00ff88';
    else if (animValue < criticalThreshold) flameColor = '#ff9f1a';

    if (intensity > 0.1) {
      for (let i = 0; i < 8; i++) {
        const layerIntensity = intensity * (1 - i * 0.1);
        const layerHeight = flameHeight * layerIntensity;
        const layerWidth = flameWidth * (1 + i * 0.15);

        ctx.beginPath();
        const wobble = Math.sin(flamePhase * 3 + i) * 5 * layerIntensity;
        
        ctx.moveTo(flameX - layerWidth / 2, flameY);
        ctx.quadraticCurveTo(
          flameX - layerWidth / 3 + wobble,
          flameY - layerHeight * 0.5,
          flameX + wobble * 0.5,
          flameY - layerHeight
        );
        ctx.quadraticCurveTo(
          flameX + layerWidth / 3 - wobble,
          flameY - layerHeight * 0.5,
          flameX + layerWidth / 2,
          flameY
        );
        ctx.closePath();

        const grad = ctx.createLinearGradient(flameX, flameY, flameX, flameY - layerHeight);
        if (i < 3) {
          grad.addColorStop(0, flameColor + '40');
          grad.addColorStop(0.5, flameColor + '80');
          grad.addColorStop(1, '#fff');
        } else {
          grad.addColorStop(0, flameColor + '20');
          grad.addColorStop(1, flameColor + '05');
        }
        ctx.fillStyle = grad;
        ctx.fill();
      }
    }

    ctx.beginPath();
    ctx.arc(flameX, flameY + 5, 15, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
    ctx.fill();

    ctx.beginPath();
    ctx.arc(flameX - 35, flameY + 15, 5, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(flameX + 35, flameY + 18, 4, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = flameColor;
    ctx.font = `bold ${width * 0.15}px "JetBrains Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.fillText(`${animValue.toFixed(1)}%`, width / 2, 30);

    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = `${width * 0.08}px "JetBrains Mono", monospace`;
    ctx.fillText(label, width / 2, height - 12);

    const barHeight = 60;
    const barX = width - 20;
    const barY = 50;

    ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
    ctx.fillRect(barX - 3, barY, 6, barHeight);

    const fillHeight = (intensity) * barHeight;
    const barGrad = ctx.createLinearGradient(barX, barY + barHeight - fillHeight, barX, barY + barHeight);
    barGrad.addColorStop(0, flameColor);
    barGrad.addColorStop(1, flameColor + '40');
    ctx.fillStyle = barGrad;
    ctx.fillRect(barX - 3, barY + barHeight - fillHeight, 6, fillHeight);
  }, [animValue, flamePhase, label, width, height, warningThreshold, criticalThreshold]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width, height }}
    />
  );
}
