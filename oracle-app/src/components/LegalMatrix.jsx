import { useState } from 'react';
import styles from './LegalMatrix.module.css';

function formatCurrency(value) {
  if (typeof value !== 'number') return value;
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

function formatFieldValue(key, value) {
  if (Array.isArray(value)) return null; // rendered separately
  if (key === 'purchase_price' || key === 'earnest_money') return formatCurrency(value);
  return String(value);
}

function formatFieldLabel(key) {
  return key
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function ContingenciesList({ items }) {
  return (
    <ul className={styles.contingencies}>
      {items.map((item, i) => (
        <li key={i} className={styles.contingencyItem}>
          {item}
        </li>
      ))}
    </ul>
  );
}

function DisclosureCategory({ category, items }) {
  const [open, setOpen] = useState(false);

  return (
    <div className={styles.disclosureGroup}>
      <button
        className={styles.categoryHeader}
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
      >
        <span className={styles.categoryName}>{category}</span>
        <span className={styles.chevron} data-open={open}>
          {open ? '−' : '+'}
        </span>
      </button>

      {open && (
        <ul className={styles.disclosureItems}>
          {items.map((item, i) => (
            <li key={i} className={styles.disclosureItem}>
              <span className={styles.disclosureDot} />
              {item}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function LegalMatrix({ legalPackage, visible }) {
  if (!legalPackage) return null;

  const {
    contract_type,
    contract_fields,
    disclosure_checklist,
    generated_at,
    agent,
  } = legalPackage;

  const formattedTime = generated_at
    ? new Date(generated_at).toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
    : '';

  return (
    <aside className={styles.panel} data-visible={visible} aria-label="Legal Matrix">
      {/* Header */}
      <div className={styles.header}>
        <span className={styles.kicker}>LEGAL MATRIX</span>
        <h2 className={styles.headline}>{contract_type}</h2>
      </div>

      {/* Scrollable body */}
      <div className={styles.body}>

        {/* Contract Fields */}
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Contract Fields</h3>
          <dl className={styles.fieldGrid}>
            {Object.entries(contract_fields).map(([key, value]) => {
              if (key === 'contingencies') return null;
              const display = formatFieldValue(key, value);
              if (display === null) return null;
              return (
                <div key={key} className={styles.fieldRow}>
                  <dt className={styles.fieldLabel}>{formatFieldLabel(key)}</dt>
                  <dd className={styles.fieldValue}>{display}</dd>
                </div>
              );
            })}
          </dl>

          {contract_fields.contingencies && (
            <div className={styles.contingenciesBlock}>
              <span className={styles.fieldLabel}>Contingencies</span>
              <ContingenciesList items={contract_fields.contingencies} />
            </div>
          )}
        </section>

        {/* Disclosure Checklist */}
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Disclosure Checklist</h3>
          <div className={styles.disclosureList}>
            {disclosure_checklist.map((entry) => (
              <DisclosureCategory
                key={entry.category}
                category={entry.category}
                items={entry.items}
              />
            ))}
          </div>
        </section>
      </div>

      {/* Footer */}
      <div className={styles.footer}>
        <span className={styles.footerTimestamp}>{formattedTime}</span>
        <span className={styles.footerAgent}>{agent}</span>
      </div>
    </aside>
  );
}
