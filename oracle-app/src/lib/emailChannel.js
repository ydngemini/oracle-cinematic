/**
 * A channel card's label (T10.09). "Ready" only when a sender has proved it
 * works: a validated account of the agent's or brokerage's own, or a platform
 * sender (the email relay, L10-11; the Twilio account for texts and calls,
 * L10-12) after a real send succeeded. Environment settings alone say a
 * platform sender is configured, not that it delivers.
 */
export function channelLabel(ready, platformState) {
  if (ready) return 'Ready';
  if (platformState === 'failing') return 'Last send failed';
  if (platformState === 'not_yet_verified') return 'Not yet verified by a send';
  return 'Not set up';
}

export const emailLabel = channelLabel;
