// Pure frontend CSV export — no dependencies. Generates an RFC-4180-safe CSV
// blob from selected lead records and triggers a browser download.

const COLUMNS = [
  ['parcel_id', 'Parcel ID'],
  ['state', 'State'],
  ['address', 'Address'],
  ['city', 'City'],
  ['owner_name', 'Owner'],
  ['owner_type', 'Owner Type'],
  ['estimated_value', 'Estimated Value'],
  ['motivation_score', 'Motivation'],
  ['is_absentee_owner', 'Absentee'],
  ['distress_flags', 'Distress Flags'],
];

function escapeCell(value) {
  if (value == null) return '';
  let s = Array.isArray(value) ? value.join('; ') : String(value);
  // Quote if the cell contains a comma, quote, or newline; double inner quotes.
  if (/[",\n\r]/.test(s)) s = `"${s.replace(/"/g, '""')}"`;
  return s;
}

export function leadsToCsv(leads) {
  const header = COLUMNS.map(([, label]) => label).join(',');
  const rows = leads.map((lead) =>
    COLUMNS.map(([key]) => escapeCell(lead[key])).join(',')
  );
  return [header, ...rows].join('\r\n');
}

export function downloadLeadsCsv(leads, filename) {
  if (!leads || leads.length === 0) return 0;
  const name = filename || `neoh-leads-${new Date().toISOString().slice(0, 10)}.csv`;
  const csv = leadsToCsv(leads);
  // Prepend BOM so Excel reads UTF-8 correctly.
  const blob = new Blob(['﻿', csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
  return leads.length;
}
