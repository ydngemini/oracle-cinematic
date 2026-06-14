// Shared CRM REST helper — one place for base URL, Bearer auth, JSON, and
// error normalization. Every tab speaks to /api/crm/* through these.
const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

function authHeaders() {
  const token = sessionStorage.getItem('oracle_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request(path, { method = 'GET', body } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...authHeaders(),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch { /* non-JSON error body — keep statusText */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }

  if (res.status === 204) return null;
  return res.json();
}

export const crmGet = (path) => request(path);
export const crmPost = (path, body) => request(path, { method: 'POST', body });
export const crmPut = (path, body) => request(path, { method: 'PUT', body });
export const crmPatch = (path, body) => request(path, { method: 'PATCH', body });
