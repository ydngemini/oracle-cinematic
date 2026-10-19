import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

/**
 * Layout and colour contracts the browser suite
 * (scripts/test-mission3-journeys-playwright.py) found broken. jsdom has no
 * layout engine, so these pin the CSS that fixed each one; the browser suite
 * is what proves the pixels.
 */

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

function block(css, selector) {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`selector not found: ${selector}`);
  const open = css.indexOf('{', start);
  return css.slice(open + 1, css.indexOf('}', open));
}

function rgba(value) {
  const m = value.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const [r, g, b, a = 1] = m[1].split(',').map((part) => Number(part.trim()));
    return { r, g, b, a };
  }
  const hex = value.match(/#([0-9a-f]{6})/i)[1];
  return { r: parseInt(hex.slice(0, 2), 16), g: parseInt(hex.slice(2, 4), 16), b: parseInt(hex.slice(4, 6), 16), a: 1 };
}

function over(fg, bg) {
  return { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 };
}

function luminance({ r, g, b }) {
  const lin = (c) => { const s = c / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(fg, bg) {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function token(css, name) {
  const m = css.match(new RegExp(`${name}:\\s*([^;]+);`));
  if (!m) throw new Error(`token not found: ${name}`);
  return m[1].trim();
}

describe('service banner vs the fixed header', () => {
  it('starts below the header instead of underneath it', () => {
    const banner = block(read('./components/ServiceStatusBanner.module.css'), '.banner {');
    expect(banner).toMatch(/margin-top:\s*calc\(var\(--crm-header-h\) \+ var\(--safe-top\)\)/);
  });

  it('stops the view reserving the header height a second time while it is shown', () => {
    const shell = read('./components/CrmShell.module.css');
    expect(block(shell, '.shellContainer:has(> [data-service-banner]) .scrollableContent'))
      .toMatch(/padding-top:\s*var\(--space-4\)/);
  });
});

describe('phone header fits 320px', () => {
  it('drops the jurisdiction label and tightens the header below 480px', () => {
    const selector = read('./components/StateSelector.module.css');
    const phone = selector.slice(selector.indexOf('@media (max-width: 479px)'));
    expect(phone).toMatch(/\.pillLabel\s*\{\s*display:\s*none/);
    expect(phone).toMatch(/min-width:\s*44px/);
    const shell = read('./components/CrmShell.module.css');
    expect(shell.slice(shell.indexOf('@media (max-width: 479px)'))).toMatch(/\.header\s*\{\s*padding-inline:\s*var\(--space-3\)/);
  });
});

describe('People fits a phone', () => {
  it('gives the People grid a shrinkable track so Opportunities cannot widen it past the screen', () => {
    expect(block(read('./components/PeopleTab.module.css'), '.wrap {')).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\)/);
  });
});

describe('Deals, Property View and the composer fit a phone', () => {
  it('gives Deals and Property View shrinkable tracks and a wrapping switcher', () => {
    for (const file of ['./components/DealsTab.module.css', './components/PropertiesTab.module.css']) {
      const css = read(file);
      expect(block(css, '.wrap {'), file).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\)/);
      const phone = css.slice(css.indexOf('@media (max-width: 680px)'));
      expect(phone, file).toMatch(/\.hero \{ grid-template-columns: minmax\(0, 1fr\)/);
      expect(phone, file).toMatch(/\.switcher \{[^}]*flex-wrap: wrap/);
    }
  });

  it('lets the conversation composer toolbar wrap', () => {
    expect(block(read('./components/CommsTab.module.css'), '.chRow {')).toMatch(/flex-wrap:\s*wrap/);
  });
});

describe('the Studio domain list keeps Check DNS on a phone', () => {
  it('outranks the later phone rules for .siteList rows instead of tying with them', () => {
    // The domain <ul> is both .siteList and .domainList. At 760px and below,
    // `.siteList li` (three tracks) and `.siteList li > button` (column 2 / -1)
    // come later with the same specificity as `.domainList li`, so they won:
    // the TXT value took an `auto` track and pushed the button past the
    // clipped edge (375px: tracks 287px 0px 0px). The compound selector wins.
    const css = read('./components/StudioTab.module.css');
    expect(css).not.toMatch(/\n\.domainList li \{/);
    expect(block(css, '.siteList.domainList li {')).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\) auto/);
    expect(block(css, '.siteList.domainList li > button {')).toMatch(/grid-column:\s*auto/);
  });
});

describe('the Neoh bar never covers a sheet', () => {
  it('sits one step below the overlay tier that sheets and dialogs use', () => {
    for (const [file, selector] of [['./neoh/NeohSurface.module.css', '.surface {'], ['./neoh/NeohConversation.module.css', '.dock {']]) {
      expect(block(read(file), selector), file).toMatch(/z-index:\s*calc\(var\(--z-overlay\) - 1\)/);
    }
  });
});

describe('faint text still reads', () => {
  it('keeps light-theme ghost text at WCAG AA on paper and on sunken surfaces', () => {
    const css = read('./index.css');
    const ghost = rgba(token(css, '--oracle-text-ghost'));
    for (const name of ['--oracle-surface', '--oracle-bg-deep', '--oracle-surface-sunken']) {
      const bg = rgba(token(css, name));
      expect(contrast(over(ghost, bg), bg), name).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe('a phone does not zoom into the house decision form', () => {
  it('keeps the reason field at 16px on a coarse pointer', () => {
    // iOS Safari zooms a focused control under 16px. index.css pins controls
    // to --text-md on a coarse pointer, but the textarea's `font` shorthand
    // (class + element) outranks that rule and resets it to the body's 14px,
    // so the module restates it after the shorthand.
    expect(read('./index.css')).toMatch(
      /@media \(pointer: coarse\) \{\s*input, textarea, select \{ font-size: var\(--text-md\); \}/,
    );
    const css = read('./components/HouseSelection.module.css');
    const base = css.indexOf('.matchForm textarea {');
    const coarse = css.indexOf('@media (pointer: coarse)', base);
    expect(base).toBeGreaterThan(-1);
    expect(coarse).toBeGreaterThan(base);
    expect(block(css.slice(coarse), '.matchForm textarea {')).toMatch(/font-size:\s*var\(--text-md\)/);
  });
});
