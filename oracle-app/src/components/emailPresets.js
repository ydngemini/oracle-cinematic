// How each common mailbox connects to Neoh. Every one of these logs in with
// the email address itself; the password is an app password wherever the
// provider offers one — an agent's normal password is refused by all of them.
//
// Checked 2026-10-07. Microsoft turned password sign-in off for personal
// Outlook.com accounts in September 2024 (OAuth only), and for Microsoft 365
// it stays available until the end of December 2026, when it is switched off
// by default and only an admin can turn it back on.

export const EMAIL_PRESETS = [
  {
    id: 'gmail',
    label: 'Gmail or Google Workspace',
    host: 'smtp.gmail.com',
    port: 587,
    passwordLabel: 'App password',
    steps: [
      'Turn on 2-Step Verification for your Google account.',
      'Create an app password at myaccount.google.com/apppasswords.',
      'Paste the 16-character app password below, not your normal Google password.',
    ],
    note: 'Can’t create an app password on a work Google account? Your admin has turned them off. Ask them to allow it.',
  },
  {
    id: 'icloud',
    label: 'iCloud Mail',
    host: 'smtp.mail.me.com',
    port: 587,
    passwordLabel: 'App-specific password',
    steps: [
      'Turn on two-factor authentication for your Apple Account.',
      'At account.apple.com, open Sign-In and Security, then App-Specific Passwords, and create one.',
      'Use your full @icloud.com address and paste that password below.',
    ],
  },
  {
    id: 'yahoo',
    label: 'Yahoo Mail',
    host: 'smtp.mail.yahoo.com',
    port: 465,
    passwordLabel: 'App password',
    steps: [
      'Open Yahoo Account Security.',
      'Choose Generate app password.',
      'Paste it below, not your normal Yahoo password.',
    ],
  },
  {
    id: 'm365',
    label: 'Microsoft 365 (work email)',
    host: 'smtp.office365.com',
    port: 587,
    passwordLabel: 'Password',
    steps: [
      'Your Microsoft 365 admin must allow Authenticated SMTP for your mailbox.',
      'Use your work email and its password (or an app password if your organization uses them).',
    ],
    note: 'Microsoft switches this off by default at the end of December 2026. After that, your admin must turn it back on.',
  },
  {
    id: 'outlook',
    label: 'Outlook.com or Hotmail (personal)',
    unsupported:
      'Microsoft no longer lets apps send from personal Outlook.com or Hotmail accounts with a password (since September 2024), so Neoh can’t connect one yet. Use another email address.',
  },
  {
    id: 'other',
    label: 'Another mail server',
    host: '',
    port: 587,
    custom: true,
    passwordLabel: 'Password',
    steps: ['Ask whoever runs your email for the SMTP server, port and login.'],
  },
];

const isAppPassword = (preset) => Boolean(preset?.passwordLabel) && preset.passwordLabel !== 'Password';

export const presetById = (id) => EMAIL_PRESETS.find((p) => p.id === id) || null;

/** The preset an address most likely belongs to, so the right steps show first. */
export function guessPreset(email) {
  const domain = String(email || '').split('@')[1]?.toLowerCase() || '';
  if (domain === 'gmail.com' || domain === 'googlemail.com') return 'gmail';
  if (['icloud.com', 'me.com', 'mac.com'].includes(domain)) return 'icloud';
  if (domain.startsWith('yahoo.') || domain === 'ymail.com') return 'yahoo';
  if (['outlook.com', 'hotmail.com', 'live.com', 'msn.com'].includes(domain)) return 'outlook';
  return null;
}

/** The fields the save endpoint takes, from what the agent typed and the preset. */
export function smtpPayload(preset, form) {
  const custom = Boolean(preset?.custom);
  const email = String(form.email || '').trim();
  return {
    account_label: form.account_label || 'default',
    host: custom ? String(form.host || '').trim() : preset.host,
    port: Number(custom ? form.port || preset.port : preset.port),
    username: (custom && String(form.username || '').trim()) || email,
    // App passwords are shown in groups ("abcd efgh ijkl mnop") and work
    // without the spaces; a real password may contain them, so keep those.
    password: isAppPassword(preset)
      ? String(form.password || '').replace(/\s+/g, '')
      : String(form.password || ''),
    from_email: email,
    ...(String(form.from_name || '').trim() ? { from_name: String(form.from_name).trim() } : {}),
  };
}

/** A login refusal in words that say what to do about it. */
export function explainEmailError(message, preset) {
  const text = String(message || '');
  if (/\b535\b|534|not accepted|authentication (failed|unsuccessful)|invalid credentials|AUTH/i.test(text)) {
    return isAppPassword(preset)
      ? `The mail server refused that login. Use an ${preset.passwordLabel.toLowerCase()} (see the steps above), not your normal password.`
      : 'The mail server refused that login. Check the email and password. Some providers need an app password.';
  }
  if (/does not resolve|non-public|timed out|timeout|refused|unreachable/i.test(text)) {
    return 'Neoh couldn’t reach that mail server. Check the server name and port.';
  }
  return text || 'That didn’t work. Check the details and try again.';
}
