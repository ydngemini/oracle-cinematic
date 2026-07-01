import { useRef, useEffect, useState } from 'react';

export default function BinaryRain({
  logs = [],
  label = 'LOG STREAM',
  width = 200,
  height = 150,
  density = 15,
  speed = 1,
}) {
  const canvasRef = useRef(null);
  const [columns, setColumns] = useState([]);

  useEffect(() => {
    const cols = Array.from({ length: Math.floor(width / 12) }, (_, i) => ({
      x: i * 12,
      chars: Array.from({ length: Math.floor(Math.random() * 10) + 5 }, () => ({
        char: Math.random() > 0.5 ? '1' : '0',
        y: Math.random() * height,
        speed: (Math.random() * 0.5 + 0.5) * speed,
        opacity: Math.random(),
      })),
    }));
    setColumns(cols);
  }, [width, height, density, speed]);

  useEffect(() => {
    let frame;
    let lastLogIndex = 0;

    const animate = () => {
      setColumns(prevCols => {
        return prevCols.map((col, colIdx) => {
          const newChars = col.chars.map(char => ({
            ...char,
            y: char.y + char.speed * 2 > height ? -20 : char.y + char.speed * 2,
            opacity: char.y > height * 0.7 ? char.opacity * 0.98 : Math.min(char.opacity + 0.02, 1),
          }));

          if (Math.random() < 0.03) {
            newChars.push({
              char: Math.random() > 0.5 ? '1' : '0',
              y: -15,
              speed: (Math.random() * 0.5 + 0.5) * speed,
              opacity: 1,
            });
          }

          return {
            ...col,
            chars: newChars.filter(c => c.opacity > 0.05),
          };
        });
      });

      frame = requestAnimationFrame(animate);
    };

    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [speed, height]);

  useEffect(() => {
    if (logs.length && columns.length) {
      const recentLogs = logs.slice(-5);
      setColumns(prevCols => {
        const newCols = [...prevCols];
        recentLogs.forEach((log, i) => {
          const colIdx = Math.floor(Math.random() * newCols.length);
          if (newCols[colIdx]) {
            const logChars = log.substring(0, 20).split('').map(c => ({
              char: c === '1' || c === '0' ? c : Math.round(Math.random()).toString(),
              y: -20 - i * 15,
              speed: (Math.random() * 0.5 + 0.5) * speed,
              opacity: 1,
            }));
            newCols[colIdx] = {
              ...newCols[colIdx],
              chars: [...newCols[colIdx].chars, ...logChars],
            };
          }
        });
        return newCols;
      });
    }
  }, [logs, speed]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.scale(dpr, dpr);

    ctx.fillStyle = 'rgba(10, 14, 20, 0.15)';
    ctx.fillRect(0, 0, width, height);

    columns.forEach(col => {
      col.chars.forEach(char => {
        const color = char.char === '1' ? '#00ff88' : '#00d4ff';
        ctx.fillStyle = color;
        ctx.globalAlpha = char.opacity;
        ctx.font = '12px "JetBrains Mono", monospace';
        ctx.fillText(char.char, col.x, char.y);
      });
    });

    ctx.globalAlpha = 1;

    const trailGrad = ctx.createLinearGradient(0, 0, 0, height * 0.15);
    trailGrad.addColorStop(0, 'rgba(10, 14, 20, 0.9)');
    trailGrad.addColorStop(1, 'rgba(10, 14, 20, 0)');
    ctx.fillStyle = trailGrad;
    ctx.fillRect(0, 0, width, height * 0.15);

    const bottomGrad = ctx.createLinearGradient(0, height * 0.85, 0, height);
    bottomGrad.addColorStop(0, 'rgba(10, 14, 20, 0)');
    bottomGrad.addColorStop(1, 'rgba(10, 14, 20, 0.9)');
    ctx.fillStyle = bottomGrad;
    ctx.fillRect(0, height * 0.85, width, height * 0.15);

    ctx.fillStyle = 'rgba(232, 238, 245, 0.5)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillText(label, 8, 14);

    ctx.fillStyle = 'rgba(0, 255, 136, 0.3)';
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.fillText(`${columns.reduce((s, c) => s + c.chars.length, 0)} chars`, width - 50, 14);
  }, [columns, label, width, height]);

  return (
    <canvas
      ref={canvasRef}
      style={{ 
        width, 
        height, 
        border: '1px solid rgba(0, 212, 255, 0.2)',
        borderRadius: '4px',
        backgroundColor: '#0a0e14',
      }}
    />
  );
}
