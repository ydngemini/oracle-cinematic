import { useState, useRef, useEffect, useCallback } from 'react';
import { SpeedGauge, HealthGauge, IndicatorLight } from '../RetroGauges.jsx';
import styles from './MatrixGridDashboard.module.css';

const defaultCards = [
  { id: 'cpu', title: 'CPU LOAD', type: 'gauge', x: 1, y: 1, w: 2, h: 2 },
  { id: 'memory', title: 'MEMORY', type: 'gauge', x: 3, y: 1, w: 2, h: 2 },
  { id: 'requests', title: 'REQUESTS', type: 'speed', x: 5, y: 1, w: 2, h: 2 },
  { id: 'instances', title: 'INSTANCES', type: 'status', x: 1, y: 3, w: 3, h: 1 },
  { id: 'cost', title: 'COST', type: 'gauge', x: 4, y: 3, w: 3, h: 1 },
];

export default function MatrixGridDashboard({ ec2, rds, metrics, billing }) {
  const [cards, setCards] = useState(defaultCards);
  const [selectedCard, setSelectedCard] = useState(null);
  const [isDragging, setIsDragging] = useState(false);
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const [isResizing, setIsResizing] = useState(false);
  const gridRef = useRef(null);

  const avgCpu = Object.values(metrics?.ec2 || {})
    .concat(Object.values(metrics?.rds || {}))
    .map(m => m?.cpu?.avg || 0)
    .filter(v => v > 0)
    .reduce((a, b, i, arr) => a + b / arr.length, 0) || 0;

  const avgMemory = Object.values(metrics?.rds || {})
    .map(m => m?.memory?.avg || 60)
    .reduce((a, b, i, arr) => a + b / arr.length, 0) || 60;

  const instanceCount = ec2.instances.length + rds.instances.length;
  const runningCount = ec2.instances.filter(i => i.state === 'running').length + 
                       rds.instances.filter(i => i.status === 'available').length;

  const handleMouseDown = useCallback((e, cardId) => {
    if (e.target.classList.contains(styles.resizeHandle)) return;
    const card = cards.find(c => c.id === cardId);
    if (!card) return;
    
    const rect = e.currentTarget.getBoundingClientRect();
    setSelectedCard(cardId);
    setIsDragging(true);
    setDragOffset({
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
    });
  }, [cards]);

  const handleResizeStart = useCallback((e, cardId) => {
    e.stopPropagation();
    setSelectedCard(cardId);
    setIsResizing(true);
  }, []);

  const handleMouseMove = useCallback((e) => {
    if (!gridRef.current) return;
    
    const rect = gridRef.current.getBoundingClientRect();
    const cellWidth = rect.width / 6;
    const cellHeight = 80;
    
    if (isDragging && selectedCard) {
      const gridX = Math.floor((e.clientX - rect.left - dragOffset.x + cellWidth / 2) / cellWidth) + 1;
      const gridY = Math.floor((e.clientY - rect.top - dragOffset.y + cellHeight / 2) / cellHeight) + 1;
      
      setCards(prev => prev.map(card => 
        card.id === selectedCard 
          ? { ...card, x: Math.max(1, Math.min(6 - card.w + 1, gridX)), y: Math.max(1, gridY) }
          : card
      ));
    }
    
    if (isResizing && selectedCard) {
      const card = cards.find(c => c.id === selectedCard);
      if (!card) return;
      
      const newW = Math.max(1, Math.min(6 - card.x + 1, Math.ceil((e.clientX - rect.left - card.x * cellWidth) / cellWidth)));
      const newH = Math.max(1, Math.min(4, Math.ceil((e.clientY - rect.top - card.y * cellHeight) / cellHeight)));
      
      setCards(prev => prev.map(c =>
        c.id === selectedCard ? { ...c, w: newW, h: newH } : c
      ));
    }
  }, [isDragging, isResizing, selectedCard, dragOffset, cards]);

  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
    setIsResizing(false);
    setSelectedCard(null);
  }, []);

  useEffect(() => {
    if (isDragging || isResizing) {
      window.addEventListener('mousemove', handleMouseMove);
      window.addEventListener('mouseup', handleMouseUp);
      return () => {
        window.removeEventListener('mousemove', handleMouseMove);
        window.removeEventListener('mouseup', handleMouseUp);
      };
    }
  }, [isDragging, isResizing, handleMouseMove, handleMouseUp]);

  const renderCardContent = (card) => {
    switch (card.type) {
      case 'gauge':
        return (
          <HealthGauge 
            value={card.id === 'cpu' ? avgCpu : card.id === 'memory' ? avgMemory : (billing?.month_total || 0)}
            label={card.title}
            color={card.id === 'cost' ? '#ff9f1a' : '#00d4ff'}
            warningThreshold={70}
            criticalThreshold={90}
          />
        );
      case 'speed':
        return (
          <SpeedGauge 
            value={instanceCount * 10 + Math.random() * 50}
            max={150}
            label={card.title}
            unit="req/s"
            color="#00ff88"
          />
        );
      case 'status':
        return (
          <div className={styles.statusCard}>
            <div className={styles.statusRow}>
              <IndicatorLight status={runningCount > 0 ? 'ok' : 'off'} label="ACTIVE" />
              <span className={styles.statusValue}>{runningCount}/{instanceCount}</span>
            </div>
            <div className={styles.statusRow}>
              <IndicatorLight status={avgCpu > 80 ? 'warning' : 'ok'} label="HEALTH" />
              <span className={styles.statusValue}>{Math.round(avgCpu)}%</span>
            </div>
          </div>
        );
      default:
        return null;
    }
  };

  return (
    <div className={styles.matrixGrid} ref={gridRef}>
      <div className={styles.gridHeader}>
        <h3>MATRIX GRID</h3>
        <div className={styles.gridControls}>
          <button className={styles.gridBtn} onClick={() => setCards(defaultCards)}>RESET</button>
        </div>
      </div>
      <div className={styles.gridContainer}>
        {cards.map(card => (
          <div
            key={card.id}
            className={`${styles.gridCard} ${selectedCard === card.id ? styles.selected : ''}`}
            style={{
              gridColumn: `${card.x} / span ${card.w}`,
              gridRow: `${card.y} / span ${card.h}`,
            }}
            onMouseDown={(e) => handleMouseDown(e, card.id)}
          >
            <div className={styles.cardHeader}>
              <span className={styles.cardTitle}>{card.title}</span>
            </div>
            <div className={styles.cardContent}>
              {renderCardContent(card)}
            </div>
            <div 
              className={styles.resizeHandle}
              onMouseDown={(e) => handleResizeStart(e, card.id)}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
