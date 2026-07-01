import { useState } from 'react';
import styles from './TabNavigator.module.css';

export default function TabNavigator({ tabs, activeTab, onTabChange }) {
  const [hoveredTab, setHoveredTab] = useState(null);

  return (
    <div className={styles.tabNavigator}>
      <div className={styles.tabBar}>
        {tabs.map((tab, index) => {
          const isActive = activeTab === tab.id;
          const isHovered = hoveredTab === tab.id;
          
          return (
            <button
              key={tab.id}
              className={`${styles.tab} ${isActive ? styles.active : ''}`}
              onClick={() => onTabChange(tab.id)}
              onMouseEnter={() => setHoveredTab(tab.id)}
              onMouseLeave={() => setHoveredTab(null)}
              disabled={tab.disabled}
            >
              <span className={styles.tabIcon}>{tab.icon || '◈'}</span>
              <span className={styles.tabLabel}>{tab.label}</span>
              {tab.badge && (
                <span className={styles.tabBadge}>{tab.badge}</span>
              )}
              {(isActive || isHovered) && !tab.disabled && (
                <div className={styles.tabGlow} />
              )}
            </button>
          );
        })}
      </div>
      <div className={styles.tabIndicator}>
        {tabs.map((tab, index) => {
          const isActive = activeTab === tab.id;
          return (
            <div 
              key={tab.id}
              className={`${styles.indicatorDot} ${isActive ? styles.activeDot : ''}`}
            />
          );
        })}
      </div>
    </div>
  );
}

export function TabContent({ children, isActive }) {
  return (
    <div className={`${styles.tabContent} ${isActive ? styles.active : ''}`}>
      {children}
    </div>
  );
}
