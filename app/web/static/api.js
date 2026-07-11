const tg = window.Telegram?.WebApp;

export class ApiError extends Error {
  constructor(message, status, code, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  if (tg?.initData) {
    headers.set('X-Telegram-Init-Data', tg.initData);
  }
  const response = await fetch(path, {
    ...options,
    headers,
    body: options.body && !(options.body instanceof FormData) ? JSON.stringify(options.body) : options.body,
  });
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload?.detail ?? payload;
    const code = typeof detail === 'object' && detail ? detail.code : `http_${response.status}`;
    const message = typeof detail === 'object' && detail ? detail.message : String(detail || response.statusText);
    throw new ApiError(message, response.status, code, detail);
  }
  return payload;
}

export const api = {
  me: () => request('/api/me'),
  status: () => request('/api/status'),
  pause: (paused) => request('/api/settings/paused', { method: 'PATCH', body: { paused } }),
  dialogs: (query = '', limit = 80, account = '') => {
    const params = new URLSearchParams({ query, limit });
    if (account) params.set('account', account);
    return request(`/api/dialogs?${params}`);
  },
  qqTargets: () => request('/api/qq-targets'),
  rules: () => request('/api/rules?limit=500'),
  rule: (id) => request(`/api/rules/${id}`),
  createRule: (payload) => request('/api/rules', { method: 'POST', body: payload }),
  updateRule: (id, payload) => request(`/api/rules/${id}`, { method: 'PATCH', body: payload }),
  duplicateRule: (id, payload = {}) => request(`/api/rules/${id}/duplicate`, { method: 'POST', body: payload }),
  setRuleEnabled: (id, enabled) => request(`/api/rules/${id}/enabled`, { method: 'PATCH', body: { enabled } }),
  deleteRule: (id) => request(`/api/rules/${id}`, { method: 'DELETE' }),
  previewRule: (payload) => request('/api/rules/preview', { method: 'POST', body: payload }),
  logs: (status = '', limit = 50) => request(`/api/logs?${new URLSearchParams({ status, limit })}`),
  options: () => request('/api/options'),
};
