import { api } from './api.js?v=20260707-rule-studio-2';

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
  rssSubscriptions: [],
  rssStatus: null,
  rssLoading: false,
  rssSubscriptionsError: null,
  rssStatusError: null,
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
  listeners.forEach((listener) => listener(state, patch));
}

export async function bootstrapData() {
  setState({ loading: true, error: null });
  const rssRefresh = refreshSubscriptions();
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
  await rssRefresh;
}

let subscriptionsRefresh = null;

export function refreshSubscriptions() {
  if (subscriptionsRefresh) return subscriptionsRefresh;
  setState({ rssLoading: true });
  subscriptionsRefresh = (async () => {
    const [subscriptions, status] = await Promise.allSettled([api.rssSubscriptions(), api.rssStatus()]);
    const patch = {
      rssLoading: false,
      rssSubscriptionsError: subscriptions.status === 'rejected' ? subscriptions.reason : null,
      rssStatusError: status.status === 'rejected' ? status.reason : null,
    };
    if (subscriptions.status === 'fulfilled') patch.rssSubscriptions = subscriptions.value;
    if (status.status === 'fulfilled') patch.rssStatus = status.value;
    setState(patch);
    return subscriptions.status === 'fulfilled' ? subscriptions.value : null;
  })().finally(() => { subscriptionsRefresh = null; });
  return subscriptionsRefresh;
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
