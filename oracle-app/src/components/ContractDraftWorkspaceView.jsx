import styles from './ContractDraftWorkspace.module.css';

const MONEY_FIELDS = new Set([
  'purchase_price', 'wholesale_buy_price', 'investor_buy_price', 'earnest_money_deposit',
]);
const LONG_TEXT_FIELDS = new Set([
  'approved_addenda', 'financing_terms', 'due_diligence_period', 'venture_purpose',
  'party_a_contribution', 'party_b_contribution', 'distribution_terms',
  'decision_authority', 'termination_terms', 'original_text', 'proposed_text',
]);

function humanize(value, fallback = 'Contract form') {
  if (!value) return fallback;
  return String(value).replaceAll('_', ' ').replaceAll('-', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Saved draft' : date.toLocaleString();
}

function inputKind(field) {
  if (field === 'current_date' || field.endsWith('_date')) return 'date';
  if (MONEY_FIELDS.has(field)) return 'number';
  return LONG_TEXT_FIELDS.has(field) ? 'textarea' : 'text';
}

export function ContractDraftWorkspaceView({
  activeTemplateKey,
  activeWorkspace,
  busyAction,
  error,
  fields,
  inputs,
  loading,
  notice,
  onChooseTemplate,
  onDownloadWorkspace,
  onFinishWithAi,
  onInput,
  onPreviewDraft,
  onResumeWorkspace,
  onSaveDraft,
  preview,
  selectedTemplate,
  surface,
  templates,
  workspaces,
}) {
  const sourcePreview = preview || selectedTemplate?.preview_text || '';
  return (
    <section className={styles.workspace} aria-labelledby={`${surface}-draft-workspace-title`} aria-busy={loading}>
      <header className={styles.header}>
        <div>
          <span className={styles.kicker}>AI draft workspace</span>
          <h2 id={`${surface}-draft-workspace-title`}>Pick, preview, save, and finish a form</h2>
          <p>Every source-controlled form is available here. Personal AI merges only known values, leaves unknown terms visible, then saves a new encrypted revision.</p>
        </div>
        <span className={styles.count}>{templates.length || '—'} forms</span>
      </header>

      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}

      {loading ? <div className={styles.skeleton} aria-hidden="true"><div /><div /></div> : (
        <>
          <div className={styles.templateGrid} aria-label="Available contract forms">
            {templates.map((template) => (
              <button
                key={template.template_key}
                className={styles.templateCard}
                data-active={template.template_key === activeTemplateKey}
                type="button"
                onClick={() => onChooseTemplate(template.template_key)}
                aria-pressed={template.template_key === activeTemplateKey}
              >
                <span>{humanize(template.document_type)}</span>
                <strong>{humanize(template.template_key)}</strong>
                <small>v{template.version} · {template.availability === 'draft_ready' ? 'Draft ready' : humanize(template.availability)}</small>
              </button>
            ))}
          </div>

          {selectedTemplate && (
            <div className={styles.editor}>
              <section className={styles.fieldPanel} aria-labelledby={`${surface}-draft-fields-title`}>
                <header>
                  <div>
                    <span>Selected source</span>
                    <h3 id={`${surface}-draft-fields-title`}>{humanize(selectedTemplate.template_key)}</h3>
                  </div>
                  <small>{selectedTemplate.jurisdiction} · v{selectedTemplate.version}</small>
                </header>

                <div className={styles.fieldGrid}>
                  {fields.map((field) => {
                    const kind = inputKind(field);
                    const fieldId = `${surface}-${selectedTemplate.template_key}-${field}`;
                    const value = inputs[field] ?? '';
                    return (
                      <label key={field} className={kind === 'textarea' ? styles.fullField : ''} htmlFor={fieldId}>
                        <span>{humanize(field)}</span>
                        {kind === 'textarea' ? (
                          <textarea id={fieldId} value={value} onChange={(event) => onInput(field, event.target.value)} maxLength={field.endsWith('_text') ? 100000 : 4000} placeholder={`Add ${humanize(field).toLowerCase()}`} />
                        ) : (
                          <input
                            id={fieldId}
                            type={kind}
                            step={kind === 'number' ? '0.01' : undefined}
                            min={kind === 'number' ? '0' : undefined}
                            value={value}
                            onChange={(event) => onInput(field, event.target.value)}
                            placeholder={`Add ${humanize(field).toLowerCase()}`}
                          />
                        )}
                      </label>
                    );
                  })}
                </div>

                <div className={styles.actions}>
                  <button type="button" onClick={onPreviewDraft} disabled={busyAction !== ''}>
                    {busyAction === 'preview' ? 'Preparing preview…' : 'Preview draft'}
                  </button>
                  <button type="button" className={styles.primaryAction} onClick={onSaveDraft} disabled={busyAction !== ''}>
                    {busyAction === 'save' ? 'Saving…' : 'Save encrypted draft'}
                  </button>
                  <button type="button" className={styles.aiAction} onClick={onFinishWithAi} disabled={busyAction !== ''}>
                    {busyAction === 'ai' ? 'Personal AI is saving…' : 'Finish with Personal AI'}
                  </button>
                  {activeWorkspace && (
                    <button type="button" onClick={() => onDownloadWorkspace(activeWorkspace)} disabled={busyAction !== ''}>
                      {busyAction === `download:${activeWorkspace.id}` ? 'Preparing PDF…' : 'Save to device'}
                    </button>
                  )}
                </div>
              </section>

              <section className={styles.previewPanel} aria-labelledby={`${surface}-draft-preview-title`}>
                <header>
                  <div>
                    <span>{preview ? 'Working draft preview' : 'Source preview'}</span>
                    <h3 id={`${surface}-draft-preview-title`}>{preview ? 'Your current draft' : 'Selected form'}</h3>
                  </div>
                  <small>{preview ? 'Draft state' : 'Source controlled'}</small>
                </header>
                <pre>{sourcePreview || 'Select a form to preview it.'}</pre>
              </section>
            </div>
          )}

          <section className={styles.saved} aria-labelledby={`${surface}-saved-drafts-title`}>
            <header>
              <div>
                <span>Encrypted backend save</span>
                <h3 id={`${surface}-saved-drafts-title`}>Saved Personal AI drafts</h3>
              </div>
              <small>{workspaces.length}</small>
            </header>
            {workspaces.length === 0 ? <p>No saved working drafts yet.</p> : (
              <ul>
                {workspaces.map((workspace) => (
                  <li key={workspace.id}>
                    <div>
                      <strong>{humanize(workspace.document_type)}</strong>
                      <small>{workspace.template_key} · {formatTime(workspace.updated_at || workspace.created_at)}</small>
                    </div>
                    <span data-status={workspace.status === 'ready' ? 'ready' : 'draft'}>{workspace.status === 'ready' ? 'Ready draft' : 'Needs inputs'}</span>
                    <div className={styles.savedActions}>
                      <button type="button" onClick={() => onResumeWorkspace(workspace.id)} disabled={busyAction !== ''}>
                        {busyAction === `resume:${workspace.id}` ? 'Loading…' : 'Resume'}
                      </button>
                      <button type="button" onClick={() => onDownloadWorkspace(workspace)} disabled={busyAction !== ''}>
                        {busyAction === `download:${workspace.id}` ? 'Preparing PDF…' : 'Save to device'}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </section>
  );
}
