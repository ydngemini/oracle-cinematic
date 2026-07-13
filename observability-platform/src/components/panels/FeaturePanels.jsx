import { useMemo, useState } from 'react';
import ListPanel from './ListPanel.jsx';
import BreakdownPanel from './BreakdownPanel.jsx';
import SummaryPanel from './SummaryPanel.jsx';
import DetailMetricsPanel from './DetailMetricsPanel.jsx';
import TimeSeriesChart from '../TimeSeriesChart.jsx';
import {
  computeMix, azDistribution, severityHistogram, severityColor, encryptionSummary,
  publicExposure, compliancePosture, isStaleUser, isOpenToWorld, openPorts,
  costTopDrivers, serviceDailySeries, fmtGb,
} from '../../lib/derive.js';

// --- small helpers --------------------------------------------------------
const stateBadge = (s) => {
  const v = (s || '').toLowerCase();
  if (['running', 'available', 'active', 'enabled', 'in-service', 'healthy', 'deployed'].includes(v)) return { text: s, className: 'running' };
  if (['stopped', 'stopping', 'failed', 'disabled', 'inactive', 'deleting', 'unhealthy'].includes(v)) return { text: s, className: 'stopped' };
  return { text: s || '—', className: 'pending' };
};
const sevBadge = (sev) => {
  const s = (sev || 'INFO').toUpperCase();
  const cls = s === 'CRITICAL' || s === 'HIGH' ? 'stopped' : s === 'MEDIUM' ? 'pending' : 'running';
  return { text: s, className: cls };
};
const dateShort = (d) => (d ? String(d).slice(0, 10) : '—');

function Group({ title, children }) {
  return (
    <>
      <div className="feature-section-title">{title}</div>
      <section className="feature-section">{children}</section>
    </>
  );
}

// Panel #18 — per-service daily cost trend with a service selector.
function CostTrendPanel({ billing }) {
  const services = useMemo(
    () => Object.keys(billing?.by_service || {}).sort((a, b) => (billing.by_service[b].total || 0) - (billing.by_service[a].total || 0)),
    [billing],
  );
  const [svc, setSvc] = useState('');
  // Validate selected service against current services list; clear stale selections
  const active = useMemo(() => {
    if (svc && services.includes(svc)) return svc;
    return services[0] || '';
  }, [svc, services]);
  const series = useMemo(() => serviceDailySeries(billing, active), [billing, active]);
  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Cost Trend</h2>
        <select
          className="range-btn"
          style={{ maxWidth: '160px' }}
          value={active}
          onChange={(e) => setSvc(e.target.value)}
        >
          {services.map((s) => (
            <option key={s} value={s}>{s.replace(/^Amazon |^AWS /, '')}</option>
          ))}
        </select>
      </div>
      <TimeSeriesChart series={series} color="var(--accent-purple)" height={90} />
    </div>
  );
}

export default function FeaturePanels({ infrastructure: infra, selectedService, onSelect }) {
  const ec2 = infra?.ec2?.instances || [];
  const rds = infra?.rds?.instances || [];
  const lambda = infra?.lambda?.functions || [];
  const ebs = infra?.ebs?.volumes || [];
  const s3 = infra?.s3?.buckets || [];
  const elb = infra?.elb?.load_balancers || [];
  const cf = infra?.cloudfront?.distributions || [];
  const asg = infra?.auto_scaling?.groups || [];
  const iam = infra?.security?.iam_users || [];
  const sgs = infra?.security?.security_groups || [];
  const findings = infra?.security?.findings || [];
  const billing = infra?.billing || {};

  const mix = useMemo(() => computeMix(infra), [infra?.ec2, infra?.rds]);
  const azDist = useMemo(() => azDistribution(infra), [infra?.ec2, infra?.rds, infra?.ebs]);
  const sevHist = useMemo(() => severityHistogram(findings), [findings]);
  const enc = useMemo(() => encryptionSummary(infra), [infra?.ebs, infra?.rds, infra?.s3]);
  const exp = useMemo(() => publicExposure(infra), [infra?.ec2, infra?.rds, infra?.security]);
  const comp = useMemo(() => compliancePosture(infra), [infra]);
  const costBuckets = useMemo(() => costTopDrivers(billing), [billing]);

  return (
    <>
      {/* ---------------- Compute ---------------- */}
      <Group title="Compute">
        <ListPanel
          className="span-2"
          title="EC2 Instances"
          items={ec2}
          rowKey={(i) => i.id}
          primary={(i) => i.name || i.id}
          badge={(i) => stateBadge(i.state)}
          meta={(i) => [i.type, i.az, i.private_ip, i.monitoring === 'enabled' ? 'monitored' : null].filter(Boolean).join(' · ')}
          onSelect={(i) => onSelect?.('ec2', i.id)}
          empty="No EC2 instances"
        />
        <ListPanel
          title="Lambda Functions"
          items={lambda}
          rowKey={(f) => f.name}
          primary={(f) => f.name}
          badge={(f) => stateBadge(f.state)}
          meta={(f) => [f.runtime, `${f.memory}MB`, `${f.timeout}s`].filter(Boolean).join(' · ')}
          onSelect={(f) => onSelect?.('lambda', f.name)}
          empty="No Lambda functions"
        />
        <ListPanel
          title="Auto Scaling Groups"
          items={asg}
          rowKey={(g) => g.name}
          primary={(g) => g.name}
          meta={(g) => `desired ${g.desired} (min ${g.min} / max ${g.max}) · ${(g.instances || []).length} instances`}
          empty="No Auto Scaling groups"
        />
        <BreakdownPanel title="Compute Mix" buckets={mix} empty="No compute resources" />
      </Group>

      {/* ---------------- Storage ---------------- */}
      <Group title="Storage">
        <ListPanel
          className="span-2"
          title="RDS Databases"
          items={rds}
          rowKey={(d) => d.id}
          primary={(d) => d.id}
          badge={(d) => stateBadge(d.status)}
          meta={(d) => [`${d.engine} ${d.engine_version || ''}`.trim(), d.class, `${d.storage}GB`, d.multi_az ? 'Multi-AZ' : 'Single-AZ', d.encrypted ? 'encrypted' : 'UNENCRYPTED'].filter(Boolean).join(' · ')}
          onSelect={(d) => onSelect?.('rds', d.id)}
          empty="No RDS databases"
        />
        <ListPanel
          title="EBS Volumes"
          items={ebs}
          rowKey={(v) => v.id}
          primary={(v) => v.id}
          badge={(v) => stateBadge(v.state)}
          meta={(v) => [`${v.size_gb}GB ${v.type}`, v.az, v.encrypted ? 'encrypted' : 'UNENCRYPTED', (v.attachments || [])[0]?.instance_id].filter(Boolean).join(' · ')}
          empty="No EBS volumes"
        />
        <ListPanel
          title="S3 Buckets"
          items={s3}
          rowKey={(b) => b.name}
          primary={(b) => b.name}
          meta={(b) => `created ${dateShort(b.created)}`}
          empty="No S3 buckets"
        />
        <SummaryPanel
          title="Storage & Encryption"
          cards={[
            { label: 'Total Storage', value: fmtGb(enc.totalGb) },
            { label: 'EBS Volumes', value: enc.volumes },
            { label: 'S3 Buckets', value: enc.s3Count },
            { label: '% Encrypted', value: `${enc.pctEncrypted}%`, className: enc.pctEncrypted >= 90 ? 'green' : enc.pctEncrypted >= 50 ? 'orange' : 'red' },
          ]}
        />
      </Group>

      {/* ---------------- Network ---------------- */}
      <Group title="Network">
        <ListPanel
          title="Load Balancers"
          items={elb}
          rowKey={(l) => l.arn || l.name}
          primary={(l) => l.name}
          badge={(l) => stateBadge(l.state)}
          meta={(l) => [`${l.type}/${l.scheme}`, `${(l.azs || []).length} AZs`, l.dns_name].filter(Boolean).join(' · ')}
          empty="No load balancers"
        />
        <ListPanel
          title="CloudFront"
          items={cf}
          rowKey={(d) => d.id}
          primary={(d) => d.domain_name || d.id}
          badge={(d) => (d.enabled ? { text: 'enabled', className: 'running' } : { text: 'disabled', className: 'stopped' })}
          meta={(d) => [d.status, d.price_class, (d.aliases || []).join(', ')].filter(Boolean).join(' · ')}
          empty="No CloudFront distributions"
        />
        <BreakdownPanel title="AZ Distribution" buckets={azDist} empty="No AZ data" />
      </Group>

      {/* ---------------- Security ---------------- */}
      <Group title="Security">
        <ListPanel
          className="span-2"
          title="Security Groups"
          items={sgs}
          rowKey={(g) => g.id}
          primary={(g) => g.name || g.id}
          badge={(g) => (isOpenToWorld(g) ? { text: 'OPEN', className: 'stopped' } : { text: 'ok', className: 'running' })}
          meta={(g) => {
            const open = openPorts(g);
            return `${(g.ingress_rules || []).length} ingress rules${open.length ? ` · world-open: ${open.join(', ')}` : ''}`;
          }}
          empty="No security groups"
        />
        <ListPanel
          title="IAM Users"
          items={iam}
          rowKey={(u) => u.arn || u.name}
          primary={(u) => u.name}
          badge={(u) => (isStaleUser(u) ? { text: 'stale', className: 'pending' } : { text: 'active', className: 'running' })}
          meta={(u) => `created ${dateShort(u.create_date)} · last used ${u.password_last_used ? dateShort(u.password_last_used) : 'never'}`}
          empty="No IAM users"
        />
        <BreakdownPanel title="Findings by Severity" buckets={sevHist} empty="No findings" />
        <ListPanel
          className="span-2"
          title="Security Findings"
          items={findings}
          rowKey={(f, i) => f.id || i}
          primary={(f) => f.title || 'Finding'}
          badge={(f) => sevBadge(f.severity)}
          meta={(f) => [f.type, (f.resources || [])[0]].filter(Boolean).join(' · ')}
          empty="✓ No active findings"
        />
        <SummaryPanel
          title="Public Exposure"
          cards={[
            { label: 'Public RDS', value: exp.publicRds, className: exp.publicRds ? 'red' : 'green' },
            { label: 'Public EC2', value: exp.publicEc2, className: exp.publicEc2 ? 'orange' : 'green' },
            { label: 'Open SGs', value: exp.openSgs, className: exp.openSgs ? 'red' : 'green' },
            { label: 'Total Exposed', value: exp.total, className: exp.total ? 'orange' : 'green' },
          ]}
        />
      </Group>

      {/* ---------------- Cost & Ops ---------------- */}
      <Group title="Cost & Ops">
        <BreakdownPanel title="Cost by Service" buckets={costBuckets} unit="$" empty="No cost data" />
        <CostTrendPanel billing={billing} />
        <SummaryPanel
          title="Compliance Posture"
          cards={[
            { label: 'Security Score', value: comp.score, className: comp.score > 80 ? 'green' : comp.score > 60 ? 'orange' : 'red' },
            { label: 'Active Findings', value: comp.findings, className: comp.findings ? 'orange' : 'green' },
            { label: '% Encrypted', value: `${comp.pctEncrypted}%`, className: comp.pctEncrypted >= 90 ? 'green' : 'orange' },
            { label: 'Exposed', value: comp.exposure, className: comp.exposure ? 'red' : 'green' },
          ]}
        />
        <DetailMetricsPanel selectedService={selectedService} />
      </Group>
    </>
  );
}
