// Pure, dependency-free derivations over the AWS snapshot (`infrastructure`).
// Everything here reads real data already present in the WS snapshot — no fetches.

const SEV_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFORMATIONAL'];
const SEV_COLOR = {
  CRITICAL: 'var(--accent-red)',
  HIGH: 'var(--accent-orange)',
  MEDIUM: 'var(--accent-cyan)',
  LOW: 'var(--accent-green)',
  INFORMATIONAL: 'var(--text-secondary)',
};

// Count array items by a key function → [{label, value}] sorted desc.
export function countBy(arr, keyFn) {
  const m = {};
  (arr || []).forEach((x) => {
    const k = keyFn(x) ?? 'unknown';
    m[k] = (m[k] || 0) + 1;
  });
  return Object.entries(m)
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value);
}

// EC2 by instance type + RDS by class → labelled buckets (panel #4).
export function computeMix(infra) {
  const ec2 = countBy(infra?.ec2?.instances, (i) => i.type).map((b) => ({ ...b, color: 'var(--accent-cyan)' }));
  const rds = countBy(infra?.rds?.instances, (i) => i.class).map((b) => ({ ...b, color: 'var(--accent-purple)' }));
  return [...ec2, ...rds];
}

// Resource count per AZ across ec2 + rds + ebs (panel #11).
export function azDistribution(infra) {
  const all = [
    ...(infra?.ec2?.instances || []).map((i) => i.az),
    ...(infra?.rds?.instances || []).map((i) => i.az),
    ...(infra?.ebs?.volumes || []).map((v) => v.az),
  ].filter(Boolean);
  return countBy(all, (az) => az).map((b) => ({ ...b, color: 'var(--accent-cyan)' }));
}

// Findings grouped by severity in fixed order + fixed colors (panel #14).
export function severityHistogram(findings) {
  const counts = {};
  (findings || []).forEach((f) => {
    const s = (f.severity || 'INFORMATIONAL').toUpperCase();
    counts[s] = (counts[s] || 0) + 1;
  });
  return SEV_ORDER.filter((s) => counts[s]).map((s) => ({
    label: s,
    value: counts[s],
    color: SEV_COLOR[s],
  }));
}

export function severityColor(sev) {
  return SEV_COLOR[(sev || '').toUpperCase()] || 'var(--text-secondary)';
}

// Encryption / storage totals from EBS + RDS (panel #8).
export function encryptionSummary(infra) {
  const vols = infra?.ebs?.volumes || [];
  const dbs = infra?.rds?.instances || [];
  const ebsGb = vols.reduce((s, v) => s + (v.size_gb || 0), 0);
  const rdsGb = dbs.reduce((s, d) => s + (d.storage || 0), 0);
  const encItems = [...vols, ...dbs];
  const encCount = encItems.filter((x) => x.encrypted).length;
  const pct = encItems.length ? Math.round((encCount / encItems.length) * 100) : 100;
  return {
    ebsGb,
    rdsGb,
    totalGb: ebsGb + rdsGb,
    s3Count: infra?.s3?.count || 0,
    volumes: vols.length,
    pctEncrypted: pct,
    encCount,
    totalEnc: encItems.length,
  };
}

// A security group is world-open if any ingress source is 0.0.0.0/0 (panels #12/#16).
export function openPorts(sg) {
  return (sg.ingress_rules || [])
    .filter((r) => (r.source || '').split(',').some((c) => c.trim() === '0.0.0.0/0'))
    .map((r) => (r.port === -1 || r.port == null ? 'ALL' : r.port));
}

export function isOpenToWorld(sg) {
  return openPorts(sg).length > 0;
}

// Public exposure across the account (panel #16).
export function publicExposure(infra) {
  const publicRds = (infra?.rds?.instances || []).filter((d) => d.publicly_accessible).length;
  const publicEc2 = (infra?.ec2?.instances || []).filter((i) => i.public_ip).length;
  const openSgs = (infra?.security?.security_groups || []).filter(isOpenToWorld).length;
  return { publicRds, publicEc2, openSgs, total: publicRds + publicEc2 + openSgs };
}

// Overall compliance posture (panel #19).
export function compliancePosture(infra) {
  const enc = encryptionSummary(infra);
  const exp = publicExposure(infra);
  return {
    score: infra?.security?.score ?? 100,
    findings: infra?.security?.findings_count || 0,
    pctEncrypted: enc.pctEncrypted,
    exposure: exp.total,
  };
}

// Flag IAM users whose password is unused or stale beyond `days` (panel #13).
export function isStaleUser(user, days = 90) {
  if (!user.password_last_used) return true;
  const last = new Date(user.password_last_used).getTime();
  if (Number.isNaN(last)) return true;
  return Date.now() - last > days * 86400000;
}

// Top cost drivers → buckets (panel #17).
export function costTopDrivers(billing, n = 8) {
  const by = billing?.by_service || {};
  return Object.entries(by)
    .map(([label, d]) => ({ label: label.replace(/^Amazon |^AWS /, ''), value: d.total || 0 }))
    .sort((a, b) => b.value - a.value)
    .slice(0, n)
    .map((b) => ({ ...b, color: 'var(--accent-purple)' }));
}

// Daily cost time-series for one service → [{t, v}] (panel #18).
export function serviceDailySeries(billing, svc) {
  const d = billing?.by_service?.[svc]?.daily || [];
  return d
    .slice()
    .sort((a, b) => (a.date < b.date ? -1 : 1))
    .map((row) => ({ t: row.date, v: row.cost }));
}

// Human-friendly bytes/GB.
export function fmtGb(gb) {
  if (gb == null) return '—';
  return gb >= 1024 ? `${(gb / 1024).toFixed(1)} TB` : `${Math.round(gb)} GB`;
}
