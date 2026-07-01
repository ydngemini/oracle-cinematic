import { useState, useMemo } from 'react';
import styles from './HierarchyTree.module.css';

const defaultHierarchy = {
  id: 'root',
  name: 'INFRASTRUCTURE',
  type: 'root',
  expanded: true,
  children: [
    {
      id: 'aws',
      name: 'AWS',
      type: 'cloud',
      expanded: false,
      children: [
        {
          id: 'ec2',
          name: 'EC2 Instances',
          type: 'service',
          expanded: false,
          children: []
        },
        {
          id: 'rds',
          name: 'RDS Databases',
          type: 'service',
          expanded: false,
          children: []
        }
      ]
    }
  ]
};

function TreeNode({ node, level = 0, onSelect, selectedId, ec2, rds, metrics }) {
  const [expanded, setExpanded] = useState(node.expanded || false);
  
  const hasChildren = node.children && node.children.length > 0;
  const isSelected = selectedId === node.id;
  
  const dynamicChildren = useMemo(() => {
    if (node.id === 'ec2' && ec2?.instances) {
      return ec2.instances.map(i => ({
        id: i.instance_id,
        name: i.instance_id,
        type: 'instance',
        state: i.state,
        children: []
      }));
    }
    if (node.id === 'rds' && rds?.instances) {
      return rds.instances.map(i => ({
        id: i.db_instance_identifier,
        name: i.db_instance_identifier,
        type: 'database',
        state: i.status,
        children: []
      }));
    }
    return node.children || [];
  }, [node, ec2, rds]);

  const getNodeIcon = () => {
    switch (node.type) {
      case 'root': return '◈';
      case 'cloud': return '☁';
      case 'service': return '▣';
      case 'instance': return '◇';
      case 'database': return '◎';
      default: return '○';
    }
  };

  const getStatusColor = () => {
    if (!node.state) return 'var(--text-secondary)';
    if (node.state === 'running' || node.state === 'available') return '#00ff88';
    if (node.state === 'pending') return '#ff9f1a';
    return '#ff3b5c';
  };

  const getMetricForNode = () => {
    if (node.type === 'instance' && metrics?.ec2?.[node.id]) {
      return metrics.ec2[node.id].cpu?.avg || 0;
    }
    if (node.type === 'database' && metrics?.rds?.[node.id]) {
      return metrics.rds[node.id].cpu?.avg || 0;
    }
    return null;
  };

  const metricValue = getMetricForNode();

  return (
    <div className={styles.treeNode} style={{ marginLeft: level * 16 }}>
      <div 
        className={`${styles.nodeContent} ${isSelected ? styles.selected : ''}`}
        onClick={() => onSelect(node)}
      >
        {hasChildren || (node.id === 'ec2' || node.id === 'rds') ? (
          <button 
            className={styles.expandBtn}
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
          >
            <span className={styles.expandIcon}>{expanded ? '▼' : '▶'}</span>
          </button>
        ) : (
          <span className={styles.expandPlaceholder} />
        )}
        
        <span className={styles.nodeIcon} style={{ color: getStatusColor() }}>
          {getNodeIcon()}
        </span>
        
        <span className={styles.nodeName}>{node.name}</span>
        
        {node.state && (
          <span className={styles.nodeState} style={{ color: getStatusColor() }}>
            {node.state.toUpperCase()}
          </span>
        )}
        
        {metricValue !== null && (
          <span className={styles.nodeMetric}>
            {Math.round(metricValue)}%
          </span>
        )}
      </div>
      
      {(expanded && dynamicChildren.length > 0) && (
        <div className={styles.children}>
          {dynamicChildren.map(child => (
            <TreeNode 
              key={child.id}
              node={child}
              level={level + 1}
              onSelect={onSelect}
              selectedId={selectedId}
              ec2={ec2}
              rds={rds}
              metrics={metrics}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function HierarchyTree({ ec2, rds, metrics, onNodeSelect }) {
  const [selectedId, setSelectedId] = useState(null);

  const handleSelect = (node) => {
    setSelectedId(node.id);
    onNodeSelect?.(node);
  };

  return (
    <div className={styles.hierarchyTree}>
      <div className={styles.treeHeader}>
        <h3>SERVICE HIERARCHY</h3>
        <div className={styles.legend}>
          <span className={styles.legendItem}>
            <span className={styles.legendDot} style={{ background: '#00ff88' }} />
            Running
          </span>
          <span className={styles.legendItem}>
            <span className={styles.legendDot} style={{ background: '#ff9f1a' }} />
            Pending
          </span>
          <span className={styles.legendItem}>
            <span className={styles.legendDot} style={{ background: '#ff3b5c' }} />
            Stopped
          </span>
        </div>
      </div>
      
      <div className={styles.treeContent}>
        <TreeNode 
          node={defaultHierarchy}
          onSelect={handleSelect}
          selectedId={selectedId}
          ec2={ec2}
          rds={rds}
          metrics={metrics}
        />
      </div>
    </div>
  );
}
