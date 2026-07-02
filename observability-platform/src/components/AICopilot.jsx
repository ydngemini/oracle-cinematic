import { useState, useEffect, useRef, useCallback } from 'react';
import { fetchCopilotReply } from '../lib/api';
import './AICopilot.css';

// Compact the live snapshot into a small JSON context the LLM can reason over
// (real numbers only — the server prompt forbids inventing resources).
function buildContext(infra, metrics, alerts) {
  if (!infra) return {};
  const topCosts = Object.entries(infra.billing?.by_service || {})
    .sort((a, b) => b[1].total - a[1].total)
    .slice(0, 5)
    .map(([service, d]) => ({ service, cost: Math.round(d.total * 100) / 100 }));
  return {
    counts: {
      ec2: infra.ec2?.count, rds: infra.rds?.count, lambda: infra.lambda?.count,
      s3: infra.s3?.count, elb: infra.elb?.count, ebs: infra.ebs?.count,
    },
    ec2_running: (infra.ec2?.instances || []).filter((i) => i.state === 'running').length,
    rds_available: (infra.rds?.instances || []).filter((i) => i.status === 'available').length,
    security_score: infra.security?.score,
    security_findings: infra.security?.findings_count,
    month_cost: infra.billing?.month_total,
    top_costs: topCosts,
    alerts: (alerts || []).slice(0, 5).map((a) => ({ sev: a.severity, msg: a.message })),
  };
}

const COPILOT_SUGGESTIONS = [
  { condition: 'cpu_high', threshold: 80, suggestion: 'CPU utilization is high. Consider scaling out or optimizing workloads.', action: 'scale_out' },
  { condition: 'cpu_critical', threshold: 95, suggestion: 'CRITICAL: CPU at capacity. Immediate action required - scale or investigate runaway process.', action: 'emergency_scale' },
  { condition: 'memory_low', threshold: 20, suggestion: 'Available memory is running low. Consider upgrading instance class or optimizing.', action: 'upgrade_instance' },
  { condition: 'error_rate_high', threshold: 10, suggestion: 'Error rate Elevated. Check application logs for root cause.', action: 'investigate_logs' },
  { condition: 'cost_spike', threshold: 150, suggestion: 'Cost anomaly detected. Review recent changes for unexpected spend.', action: 'cost_audit' },
  { condition: 'security_critical', threshold: 1, suggestion: 'CRITICAL security finding. Immediate remediation recommended.', action: 'security_remediate' },
  { condition: 'latency_high', threshold: 500, suggestion: 'Latency above threshold. Consider performance optimization.', action: 'perf_tune' },
  { condition: 'connections_high', threshold: 80, suggestion: 'Database connections approaching max. Consider connection pooling or scale.', action: 'db_scale' },
];

export default function AICopilot({ infrastructure, metrics, alerts }) {
  const [suggestions, setSuggestions] = useState([]);
  const [chatOpen, setChatOpen] = useState(false);
  const [chatInput, setChatInput] = useState('');
  const [chatHistory, setChatHistory] = useState([]);
  const [isTyping, setIsTyping] = useState(false);
  const chatEndRef = useRef(null);

  const analyzeInfra = useCallback(() => {
    const newSuggestions = [];

    if (!infrastructure) return;

    Object.entries(metrics?.ec2 || {}).forEach(([id, data]) => {
      const cpu = data?.cpu?.avg || 0;
      if (cpu > 95) {
        newSuggestions.push({
          id: `ec2-${id}-critical`,
          type: 'critical',
          resource: id,
          message: `EC2 ${id} CPU at ${cpu.toFixed(1)}% - CRITICAL`,
          suggestion: 'Immediate scaling recommended. Instance may become unresponsive.',
          action: 'scale',
          timestamp: new Date().toISOString(),
        });
      } else if (cpu > 80) {
        newSuggestions.push({
          id: `ec2-${id}-warning`,
          type: 'warning',
          resource: id,
          message: `EC2 ${id} CPU at ${cpu.toFixed(1)}%`,
          suggestion: 'Consider adding instances to the Auto Scaling Group.',
          action: 'scale_out',
          timestamp: new Date().toISOString(),
        });
      }
    });

    Object.entries(metrics?.rds || {}).forEach(([id, data]) => {
      const cpu = data?.cpu?.avg || 0;
      if (cpu > 90) {
        newSuggestions.push({
          id: `rds-${id}-critical`,
          type: 'critical',
          resource: id,
          message: `RDS ${id} CPU at ${cpu.toFixed(1)}% - HIGH LOAD`,
          suggestion: 'Database under heavy load. Consider read replicas or query optimization.',
          action: 'db_optimize',
          timestamp: new Date().toISOString(),
        });
      }
    });

    if (alerts?.length > 0) {
      const criticalAlerts = alerts.filter(a => a.severity === 'critical');
      if (criticalAlerts.length > 0) {
        newSuggestions.push({
          id: 'alerts-critical',
          type: 'critical',
          resource: 'system',
          message: `${criticalAlerts.length} critical alert(s) active`,
          suggestion: 'Review and acknowledge alerts before they impact service.',
          action: 'view_alerts',
          timestamp: new Date().toISOString(),
        });
      }
    }

    const billing = infrastructure?.billing;
    if (billing?.month_total > 1000) {
      newSuggestions.push({
        id: 'cost-monitoring',
        type: 'info',
        resource: 'billing',
        message: `Monthly spend: $${billing.month_total.toFixed(2)}`,
        suggestion: 'Cost monitoring active. Review Reserved Instances for savings.',
        action: 'cost_analysis',
        timestamp: new Date().toISOString(),
      });
    }

    const security = infrastructure?.security;
    if (security?.findings_count > 0) {
      newSuggestions.push({
        id: 'security-findings',
        type: 'warning',
        resource: 'security',
        message: `${security.findings_count} security finding(s)`,
        suggestion: 'Review Security Hub for remediation steps.',
        action: 'security_audit',
        timestamp: new Date().toISOString(),
      });
    }

    setSuggestions(newSuggestions.slice(0, 5));
  }, [infrastructure, metrics, alerts]);

  useEffect(() => {
    analyzeInfra();
    const interval = setInterval(analyzeInfra, 30000);
    return () => clearInterval(interval);
  }, [analyzeInfra]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatHistory]);

  const handleChatSubmit = async (e) => {
    e?.preventDefault();
    const userMessage = chatInput.trim();
    if (!userMessage || isTyping) return;

    setChatInput('');
    setChatHistory(prev => [...prev, { role: 'user', content: userMessage }]);
    setIsTyping(true);

    try {
      // Real LLM (Bedrock) with the live infra snapshot as context.
      const { reply } = await fetchCopilotReply(userMessage, buildContext(infrastructure, metrics, alerts));
      const content = reply || generateCopilotResponse(userMessage, infrastructure, metrics);
      setChatHistory(prev => [...prev, { role: 'assistant', content }]);
    } catch {
      // LLM unreachable → graceful fallback to the rule-based responder.
      setChatHistory(prev => [...prev, { role: 'assistant', content: generateCopilotResponse(userMessage, infrastructure, metrics) }]);
    } finally {
      setIsTyping(false);
    }
  };

  const handleActionClick = (suggestion) => {
    setChatHistory(prev => [...prev, 
      { role: 'assistant', content: `Analyzing ${suggestion.resource}...` },
      { role: 'assistant', content: suggestion.suggestion }
    ]);
    setChatOpen(true);
  };

  const urgentCount = suggestions.filter(s => s.type === 'critical').length;
  const warningCount = suggestions.filter(s => s.type === 'warning').length;

  return (
    <div className="ai-copilot">
      <div
        className="copilot-toggle"
        role="button"
        tabIndex={0}
        aria-label="Toggle NAVI copilot"
        aria-expanded={chatOpen}
        onClick={() => setChatOpen(!chatOpen)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setChatOpen(!chatOpen);
          }
        }}
      >
        <div className={`copilot-avatar ${urgentCount > 0 ? 'urgent' : warningCount > 0 ? 'warning' : 'ok'}`}>
          <span className="copilot-icon">◈</span>
          {urgentCount > 0 && <span className="copilot-badge critical">{urgentCount}</span>}
          {warningCount > 0 && urgentCount === 0 && <span className="copilot-badge warning">{warningCount}</span>}
        </div>
        <span className="copilot-label">NAVI</span>
      </div>

      {chatOpen && (
        <div className="copilot-panel">
          <div className="copilot-header">
            <h3>◈ NAVI — AI Copilot</h3>
            <button className="copilot-close" onClick={() => setChatOpen(false)}>×</button>
          </div>

          {suggestions.length > 0 && (
            <div className="copilot-suggestions">
              <h4>Recommendations</h4>
              {suggestions.map(s => (
                <div key={s.id} className={`suggestion-item ${s.type}`} onClick={() => handleActionClick(s)}>
                  <span className="suggestion-icon">{s.type === 'critical' ? '⚡' : s.type === 'warning' ? '⚠' : '💡'}</span>
                  <div className="suggestion-content">
                    <div className="suggestion-message">{s.message}</div>
                    <div className="suggestion-detail">{s.suggestion}</div>
                  </div>
                  <span className="suggestion-action">→</span>
                </div>
              ))}
            </div>
          )}

          <div className="copilot-chat">
            <div className="chat-messages">
              {chatHistory.length === 0 && (
                <div className="chat-welcome">
                  <p>Hi Captain! I'm NAVI, your cloud copilot.</p>
                  <p>Ask me about your infrastructure, costs, or performance.</p>
                </div>
              )}
              {chatHistory.map((msg, i) => (
                <div key={i} className={`chat-message ${msg.role}`}>
                  {msg.content}
                </div>
              ))}
              {isTyping && <div className="chat-message assistant typing">...</div>}
              <div ref={chatEndRef} />
            </div>
            <form onSubmit={handleChatSubmit} className="chat-input-form">
              <input
                type="text"
                value={chatInput}
                onChange={e => setChatInput(e.target.value)}
                placeholder="Ask NAVI..."
                className="chat-input"
              />
              <button type="submit" className="chat-submit">→</button>
            </form>
          </div>

          <div className="copilot-status">
            <span className="status-item">
              <span className="status-dot green" />
              {infrastructure?.ec2?.count || 0} EC2
            </span>
            <span className="status-item">
              <span className="status-dot cyan" />
              {infrastructure?.rds?.count || 0} RDS
            </span>
            <span className="status-item">
              <span className="status-dot purple" />
              ${infrastructure?.billing?.month_total?.toFixed(2) || '0.00'}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

function generateCopilotResponse(query, infra, metrics) {
  const q = query.toLowerCase();

  if (q.includes('cpu') || q.includes('load')) {
    const avgCpu = Object.values(metrics?.ec2 || {})
      .map(m => m?.cpu?.avg || 0)
      .filter(v => v > 0)
      .reduce((a, b) => a + b, 0) / Math.max(1, Object.keys(metrics?.ec2 || {}).length);
    
    if (avgCpu > 80) {
      return `Average CPU across your EC2 fleet is ${avgCpu.toFixed(1)}%. I recommend scaling out your Auto Scaling Group to handle the load.`;
    }
    return `Average CPU utilization is ${avgCpu.toFixed(1)}%. Your compute resources look healthy.`;
  }

  if (q.includes('cost') || q.includes('spend') || q.includes('bill')) {
    const total = infra?.billing?.month_total || 0;
    return `Your current month-to-date spend is $${total.toFixed(2)}. The top services by cost are: ${Object.entries(infra?.billing?.by_service || {}).slice(0, 3).map(([k, v]) => `${k} ($${v.total.toFixed(2)})`).join(', ')}. Would you like cost optimization recommendations?`;
  }

  if (q.includes('scale') || q.includes('capacity')) {
    return `I can help you scale your resources. You currently have ${infra?.auto_scaling?.count || 0} Auto Scaling Groups. Would you like to adjust capacity? Use the Drive controls to make changes.`;
  }

  if (q.includes('security') || q.includes('compliance')) {
    const findings = infra?.security?.findings_count || 0;
    const score = infra?.security?.score || 100;
    return `Security posture: ${score}/100. You have ${findings} finding(s) in Security Hub. ${findings > 0 ? 'I recommend reviewing critical findings first.' : 'All clear!'}`;
  }

  if (q.includes('database') || q.includes('rds') || q.includes('db')) {
    const rdsCount = infra?.rds?.count || 0;
    return `You have ${rdsCount} RDS instance(s). ${rdsCount > 0 ? 'Would you like performance metrics?' : 'No databases detected.'}`;
  }

  if (q.includes('help') || q.includes('what can')) {
    return 'I can help you with: monitoring CPU/memory usage, analyzing costs, scaling resources, security posture, and database performance. Just ask!';
  }

  return `I'm analyzing your infrastructure. You have ${infra?.ec2?.count || 0} EC2 instances, ${infra?.rds?.count || 0} RDS databases, and a monthly spend of $${infra?.billing?.month_total?.toFixed(2) || '0.00'}. What would you like to know more about?`;
}
