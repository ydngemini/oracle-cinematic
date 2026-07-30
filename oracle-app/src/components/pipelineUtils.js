// Contract clock zones mirror the disposition service's temporal safeguards.
export function contractCountdown(lead) {
  if (!lead.contract_expires_at) return null;
  if (!['under_contract', 'marketing'].includes(lead.dossier_status)) return null;
  const days = Math.max(
    0,
    Math.ceil((new Date(lead.contract_expires_at) - Date.now()) / 86_400_000)
  );
  return { days, zone: days <= 15 ? 'danger' : days <= 30 ? 'warn' : 'calm' };
}
