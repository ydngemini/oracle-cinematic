/** The plan price as GET /billing/plan reports it (read from the Stripe Price
 *  checkout bills), or null when it can't be read: never a guessed figure. */
export function formatPlanPrice(plan) {
  if (!plan || plan.source !== 'stripe' || typeof plan.amount_cents !== 'number') return null;
  const amount = new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: (plan.currency || 'usd').toUpperCase(),
    minimumFractionDigits: plan.amount_cents % 100 === 0 ? 0 : 2,
  }).format(plan.amount_cents / 100);
  const every = plan.interval_count && plan.interval_count > 1 ? `${plan.interval_count} ${plan.interval}s` : plan.interval;
  return { amount, period: every ? `per ${every}` : '' };
}
