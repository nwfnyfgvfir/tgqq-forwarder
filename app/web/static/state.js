import { api } from './api.js';

export const state = {
  route: 'dashboard',
  routeParams: {},
  me: null,
  status: null,
  rules: [],
  logs: [],
  options: null,
  dialogs: [],
  qqTargets: [],
  loading: false,
  error: null,
};

const listeners = new Set();

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function setState(patch) {
  Object.assign(state, patch);
  listeners.forEach((listener) => listener(state));
}

export async function bootstrapData() {
  setState({ loading: true, error: null });
  try {
    const [me, status, rules, logs, options, qqTargets] = await Promise.all([
      api.me(),
      api.status(),
      api.rules(),
      api.logs('', 30),
      api.options(),
      api.qqTargets(),
    ]);
    setState({ me, status, rules, logs, options, qqTargets, loading: false });
  } catch (error) {
    setState({ error, loading: false });
  }
}

export async function refreshStatus() {
  const status = await api.status();
  setState({ status });
  return status;
}

export async function refreshRules() {
  const rules = await api.rules();
  setState({ rules });
  return rules;
}

export async function refreshLogs(statusFilter = '') {
  const logs = await api.logs(statusFilter, 50);
  setState({ logs });
  return logs;
}

export function navigate(route, routeParams = {}) {
  setState({ route, routeParams, error: null });
}
