import { useState, useRef } from 'react';
import './MobileNav.css';

export default function MobileNav({ activeView, onViewChange, views }) {
  const defaultViews = [
    { id: 'dashboard', label: 'Dash', icon: '◉' },
    { id: 'metrics', label: 'Metrics', icon: '◐' },
    { id: 'topology', label: 'Map', icon: '◈' },
    { id: 'alerts', label: 'Alerts', icon: '⚡' },
    { id: 'more', label: 'More', icon: '☰' },
  ];
  
  const navViews = views || defaultViews;

  return (
    <nav className="mobile-nav">
      {navViews.map(view => (
        <button
          type="button"
          key={view.id}
          className={`mobile-nav-btn ${activeView === view.id ? 'active' : ''}`}
          onClick={() => onViewChange(view.id)}
          aria-current={activeView === view.id ? 'page' : undefined}
        >
          <span className="mobile-nav-icon">{view.icon}</span>
          <span className="mobile-nav-label">{view.label}</span>
        </button>
      ))}
    </nav>
  );
}

export function SwipeContainer({ children, views, activeIndex, onIndexChange }) {
  const [startX, setStartX] = useState(0);
  const [currentX, setCurrentX] = useState(0);
  const [isDragging, setIsDragging] = useState(false);

  const handleTouchStart = (e) => {
    setStartX(e.touches[0].clientX);
    setIsDragging(true);
  };

  const handleTouchMove = (e) => {
    if (!isDragging) return;
    setCurrentX(e.touches[0].clientX);
  };

  const handleTouchEnd = () => {
    if (!isDragging) return;
    setIsDragging(false);
    
    const diff = startX - currentX;
    const threshold = 50;

    if (Math.abs(diff) > threshold) {
      if (diff > 0 && activeIndex < views.length - 1) {
        onIndexChange(activeIndex + 1);
      } else if (diff < 0 && activeIndex > 0) {
        onIndexChange(activeIndex - 1);
      }
    }
    setCurrentX(0);
  };

  return (
    <div
      className="swipe-container"
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
    >
      {children}
    </div>
  );
}

export function PullToRefresh({ onRefresh, children }) {
  const [isPulling, setIsPulling] = useState(false);
  const [pullDistance, setPullDistance] = useState(0);
  const [isRefreshing, setIsRefreshing] = useState(false);
  // Mutable ref, NOT state: we write .current in touch handlers without re-rendering.
  // (Was useState(0) — its .current was undefined after the first re-render, which
  // made `diff` NaN and silently disabled pull-to-refresh.)
  const startY = useRef(0);

  const handleTouchStart = (e) => {
    if (e.currentTarget.scrollTop === 0) {
      startY.current = e.touches[0].clientY;
      setIsPulling(true);
    }
  };

  const handleTouchMove = (e) => {
    if (!isPulling) return;
    const currentY = e.touches[0].clientY;
    const diff = currentY - startY.current;
    if (diff > 0) {
      setPullDistance(Math.min(diff, 100));
    }
  };

  const handleTouchEnd = async () => {
    if (pullDistance > 60) {
      setIsRefreshing(true);
      await onRefresh();
      setIsRefreshing(false);
    }
    setIsPulling(false);
    setPullDistance(0);
  };

  return (
    <div
      className="pull-to-refresh"
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
      style={{ transform: `translateY(${pullDistance * 0.3}px)` }}
    >
      {(pullDistance > 20 || isRefreshing) && (
        <div className="pull-refresh-indicator">
          {isRefreshing ? '↻ Refreshing...' : '↓ Pull to refresh'}
        </div>
      )}
      {children}
    </div>
  );
}
