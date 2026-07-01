import { useState, useRef, useEffect } from 'react';
import styles from './ChronoPanel.module.css';

export default function ChronoPanel({ onTimeChange, isPlaying: externalPlaying, onPlayPause }) {
  const [currentTime, setCurrentTime] = useState(Date.now());
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const [timeWindow, setTimeWindow] = useState('1h');
  const [isDragging, setIsDragging] = useState(false);
  const scrubberRef = useRef(null);
  const animationRef = useRef(null);

  const timeWindows = [
    { value: '15m', label: '15 MIN' },
    { value: '1h', label: '1 HR' },
    { value: '6h', label: '6 HR' },
    { value: '24h', label: '24 HR' },
    { value: '7d', label: '7 DAY' },
  ];

  const getWindowDuration = (window) => {
    const durations = {
      '15m': 15 * 60 * 1000,
      '1h': 60 * 60 * 1000,
      '6h': 6 * 60 * 60 * 1000,
      '24h': 24 * 60 * 60 * 1000,
      '7d': 7 * 24 * 60 * 60 * 1000,
    };
    return durations[window] || durations['1h'];
  };

  const formatTime = (timestamp) => {
    const date = new Date(timestamp);
    return date.toLocaleTimeString('en-US', { 
      hour: '2-digit', 
      minute: '2-digit',
      second: '2-digit',
      hour12: false 
    });
  };

  const formatDate = (timestamp) => {
    const date = new Date(timestamp);
    return date.toLocaleDateString('en-US', { 
      month: 'short', 
      day: 'numeric' 
    });
  };

  useEffect(() => {
    if (isPlaying && !isDragging) {
      const startTime = currentTime;
      const startRealTime = performance.now();
      
      const animate = (realTime) => {
        const elapsed = (realTime - startRealTime) * playbackSpeed;
        const newTime = startTime + elapsed;
        setCurrentTime(newTime);
        onTimeChange?.(newTime);
        animationRef.current = requestAnimationFrame(animate);
      };
      
      animationRef.current = requestAnimationFrame(animate);
      
      return () => {
        if (animationRef.current) {
          cancelAnimationFrame(animationRef.current);
        }
      };
    }
  }, [isPlaying, playbackSpeed, isDragging, onTimeChange]);

  const handlePlayPause = () => {
    const newPlaying = !isPlaying;
    setIsPlaying(newPlaying);
    onPlayPause?.(newPlaying);
  };

  const handleScrubberClick = (e) => {
    if (!scrubberRef.current) return;
    
    const rect = scrubberRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const percent = x / rect.width;
    
    const windowDuration = getWindowDuration(timeWindow);
    const windowStart = Date.now() - windowDuration;
    const newTime = windowStart + percent * windowDuration;
    
    setCurrentTime(newTime);
    onTimeChange?.(newTime);
  };

  const handleMouseDown = (e) => {
    setIsDragging(true);
    handleScrubberClick(e);
  };

  const handleMouseMove = (e) => {
    if (!isDragging) return;
    handleScrubberClick(e);
  };

  const handleMouseUp = () => {
    setIsDragging(false);
  };

  useEffect(() => {
    if (isDragging) {
      window.addEventListener('mousemove', handleMouseMove);
      window.addEventListener('mouseup', handleMouseUp);
      return () => {
        window.removeEventListener('mousemove', handleMouseMove);
        window.removeEventListener('mouseup', handleMouseUp);
      };
    }
  }, [isDragging]);

  const windowDuration = getWindowDuration(timeWindow);
  const windowStart = Date.now() - windowDuration;
  const scrubPercent = ((currentTime - windowStart) / windowDuration) * 100;

  return (
    <div className={styles.chronoPanel}>
      <div className={styles.chronoHeader}>
        <h3>TIME SCRUBBER</h3>
        <div className={styles.timeDisplay}>
          <span className={styles.dateText}>{formatDate(currentTime)}</span>
          <span className={styles.timeText}>{formatTime(currentTime)}</span>
        </div>
      </div>

      <div className={styles.controls}>
        <button 
          className={`${styles.playBtn} ${isPlaying ? styles.playing : ''}`}
          onClick={handlePlayPause}
        >
          {isPlaying ? '❚❚' : '▶'}
        </button>

        <div className={styles.speedControls}>
          {[0.5, 1, 2, 4].map(speed => (
            <button
              key={speed}
              className={`${styles.speedBtn} ${playbackSpeed === speed ? styles.active : ''}`}
              onClick={() => setPlaybackSpeed(speed)}
            >
              {speed}x
            </button>
          ))}
        </div>

        <button 
          className={styles.resetBtn}
          onClick={() => {
            setCurrentTime(Date.now());
            onTimeChange?.(Date.now());
          }}
        >
          NOW
        </button>
      </div>

      <div className={styles.windowSelector}>
        {timeWindows.map(window => (
          <button
            key={window.value}
            className={`${styles.windowBtn} ${timeWindow === window.value ? styles.active : ''}`}
            onClick={() => setTimeWindow(window.value)}
          >
            {window.label}
          </button>
        ))}
      </div>

      <div 
        className={styles.scrubber}
        ref={scrubberRef}
        onMouseDown={handleMouseDown}
      >
        <div className={styles.scrubberTrack}>
          <div 
            className={styles.scrubberFill}
            style={{ width: `${Math.max(0, Math.min(100, scrubPercent))}%` }}
          />
        </div>
        <div 
          className={styles.scrubberHandle}
          style={{ left: `${Math.max(0, Math.min(100, scrubPercent))}%` }}
        />
        
        <div className={styles.timeMarkers}>
          {[0, 25, 50, 75, 100].map(percent => {
            const markerTime = windowStart + (percent / 100) * windowDuration;
            return (
              <span key={percent} className={styles.timeMarker}>
                {formatTime(markerTime)}
              </span>
            );
          })}
        </div>
      </div>

      <div className={styles.timelineHints}>
        <span className={styles.hint}>◀ PAST</span>
        <span className={styles.hint}>PRESENT ▶</span>
      </div>
    </div>
  );
}
