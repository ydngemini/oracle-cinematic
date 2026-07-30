import { useEffect, useMemo, useState } from 'react';
import { crmDownload, crmGet, crmPost } from '../state/useCrmApi';
import { ContractDraftWorkspaceView } from './ContractDraftWorkspaceView';

export function ContractDraftWorkspace({ surface = 'contracts' }) {
  const [templates, setTemplates] = useState([]);
  const [workspaces, setWorkspaces] = useState([]);
  const [selectedKey, setSelectedKey] = useState('');
  const [inputs, setInputs] = useState({});
  const [preview, setPreview] = useState('');
  const [activeWorkspace, setActiveWorkspace] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    let active = true;
    void Promise.allSettled([
      crmGet('/api/contracts/templates/library'),
      crmGet('/api/contracts/draft-workspaces?limit=12'),
    ]).then(([libraryResult, workspaceResult]) => {
      if (!active) return;
      if (libraryResult.status === 'fulfilled') {
        setTemplates(Array.isArray(libraryResult.value?.templates) ? libraryResult.value.templates : []);
      } else {
        setError('The contract form library is unavailable. Refresh to retry.');
      }
      if (workspaceResult.status === 'fulfilled') {
        setWorkspaces(Array.isArray(workspaceResult.value?.workspaces) ? workspaceResult.value.workspaces : []);
      }
      setLoading(false);
    });
    return () => { active = false; };
  }, []);

  const selectedTemplate = useMemo(
    () => templates.find((template) => template.template_key === selectedKey) || templates[0] || null,
    [selectedKey, templates],
  );
  const activeTemplateKey = selectedTemplate?.template_key || '';
  const fields = selectedTemplate?.required_fields || [];

  const chooseTemplate = (templateKey) => {
    setSelectedKey(templateKey);
    setInputs({});
    setPreview('');
    setActiveWorkspace(null);
    setError('');
    setNotice('');
  };

  const setInput = (field, value) => {
    setInputs((current) => ({ ...current, [field]: value }));
    setNotice('');
  };

  const previewDraft = async () => {
    if (!selectedTemplate) return;
    setBusyAction('preview');
    setError('');
    try {
      const result = await crmPost('/api/contracts/draft-workspaces/preview', {
        template_key: selectedTemplate.template_key,
        inputs,
      });
      setPreview(result?.editable_draft || '');
      setNotice(result?.assistant?.missing_fields?.length
        ? `${result.assistant.missing_fields.length} fields remain visibly marked for Personal AI.`
        : 'Preview is ready with all required values supplied.');
    } catch (requestError) {
      setError(requestError.message || 'The draft preview could not be created.');
    } finally {
      setBusyAction('');
    }
  };

  const saveDraft = async () => {
    if (!selectedTemplate) return null;
    setBusyAction('save');
    setError('');
    try {
      const result = await crmPost('/api/contracts/draft-workspaces', {
        template_key: selectedTemplate.template_key,
        inputs,
      });
      setActiveWorkspace(result.workspace);
      setPreview(result.editable_draft || '');
      setWorkspaces((current) => [result.workspace, ...current.filter((item) => item.id !== result.workspace.id)]);
      setNotice('Encrypted working draft saved on the backend.');
      return result.workspace;
    } catch (requestError) {
      setError(requestError.message || 'The working draft could not be saved.');
      return null;
    } finally {
      setBusyAction('');
    }
  };

  const finishWithAi = async () => {
    let workspace = activeWorkspace;
    if (!workspace?.id) workspace = await saveDraft();
    if (!workspace?.id) return;
    setBusyAction('ai');
    setError('');
    try {
      const result = await crmPost(
        `/api/contracts/draft-workspaces/${encodeURIComponent(workspace.id)}/ai-complete`,
        { inputs },
      );
      setActiveWorkspace(result.workspace);
      setPreview(result.editable_draft || '');
      setWorkspaces((current) => [result.workspace, ...current.filter((item) => item.id !== result.workspace.id)]);
      setNotice(result.assistant?.missing_fields?.length
        ? 'Personal AI saved a new encrypted revision and kept unknown fields visible.'
        : 'Personal AI saved the completed working draft.');
    } catch (requestError) {
      setError(requestError.message || 'Personal AI could not save this draft revision.');
    } finally {
      setBusyAction('');
    }
  };

  const resumeWorkspace = async (workspaceId) => {
    setBusyAction(`resume:${workspaceId}`);
    setError('');
    try {
      const result = await crmGet(`/api/contracts/draft-workspaces/${encodeURIComponent(workspaceId)}`);
      setSelectedKey(result.workspace.template_key);
      setInputs(result.inputs || {});
      setPreview(result.editable_draft || '');
      setActiveWorkspace(result.workspace);
      setNotice('Saved draft loaded into Personal AI.');
    } catch (requestError) {
      setError(requestError.message || 'The saved draft could not be loaded.');
    } finally {
      setBusyAction('');
    }
  };

  const downloadWorkspace = async (workspace) => {
    setBusyAction(`download:${workspace.id}`);
    setError('');
    try {
      await crmDownload(
        `/api/contracts/draft-workspaces/${encodeURIComponent(workspace.id)}/download`,
        `neoh-${String(workspace.document_type || 'contract').replaceAll('_', '-')}-draft.pdf`,
      );
      setNotice('Draft PDF sent to your device.');
    } catch (requestError) {
      setError(requestError.message || 'The draft PDF could not be downloaded.');
    } finally {
      setBusyAction('');
    }
  };

  return (
    <ContractDraftWorkspaceView
      activeTemplateKey={activeTemplateKey}
      activeWorkspace={activeWorkspace}
      busyAction={busyAction}
      error={error}
      fields={fields}
      inputs={inputs}
      loading={loading}
      notice={notice}
      onChooseTemplate={chooseTemplate}
      onDownloadWorkspace={downloadWorkspace}
      onFinishWithAi={finishWithAi}
      onInput={setInput}
      onPreviewDraft={previewDraft}
      onResumeWorkspace={resumeWorkspace}
      onSaveDraft={saveDraft}
      preview={preview}
      selectedTemplate={selectedTemplate}
      surface={surface}
      templates={templates}
      workspaces={workspaces}
    />
  );
}
