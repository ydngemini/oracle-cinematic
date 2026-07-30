import {
  fetchWithRetry,
  fetchBlob,
  uploadFile,
} from '../lib/apiClient';

export { ApiError } from '../lib/apiClient';

export async function crmGet(path, options) {
  return fetchWithRetry(path, { ...options, method: 'GET' });
}

export async function crmPost(path, body, options) {
  return fetchWithRetry(path, { ...options, method: 'POST', body });
}

export async function crmPut(path, body, options) {
  return fetchWithRetry(path, { ...options, method: 'PUT', body });
}

export async function crmPatch(path, body, options) {
  return fetchWithRetry(path, { ...options, method: 'PATCH', body });
}

export async function crmDelete(path, options) {
  return fetchWithRetry(path, { ...options, method: 'DELETE' });
}

export async function crmGetBlob(path, options) {
  return fetchBlob(path, options);
}

export async function crmDownload(path, filename, options) {
  const blob = await fetchBlob(path, options);
  const objectUrl = URL.createObjectURL(blob);
  const link = window.document.createElement('a');
  link.href = objectUrl;
  link.download = filename;
  link.style.display = 'none';
  window.document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

export async function crmUpload(path, formData, options) {
  return uploadFile(path, formData, options);
}
