/**
 * The Email card (T10.09, L10-11). "Ready" only when a sender has proved it
 * works: a validated mail server of the agent's or brokerage's own, or the
 * platform relay after a real send succeeded. Environment settings alone say
 * the relay is configured, not that it delivers.
 */
export function emailLabel(ready, platformState) {
  if (ready) return 'Ready';
  if (platformState === 'failing') return 'Last send failed';
  if (platformState === 'not_yet_verified') return 'Not yet verified by a send';
  return 'Not set up';
}
