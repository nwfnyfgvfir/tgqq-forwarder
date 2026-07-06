import { api } from './api.js';
import { renderShell } from './components.js';
import { bootstrapData, navigate, refreshLogs, refreshRules, refreshStatus, setState, state, subscribe } from './state.js';
import { renderDashboard } from './views/dashboard.js';
import { renderLogs } from './views/logs.js';
import { filterRules, renderRules } from './views/rules.js';
import { bindRuleStudio, renderRuleStudio } from './views/rule-studio.js';
import { renderSystem } from './views/system.js';

const tg = window.Telegram?.WebApp;
const root = document.querySelector('#app');

function initTelegramShell() {
  if (!tg) return;
  tg.ready();
  tg.expand();
  tg.setHeaderColor?.('#07100f');
  tg.setBackgroundColor?.('#07100f');
  tg.BackButton.onClick(() => navigate('dashboard'));
}

function applyTelegramTheme() {
  const params = tg?.themeParams || {};
  const rootStyle = document.documentElement.style;
  if (params.bg_color) rootStyle.setProperty('--tg-bg', params.bg_color);
  if (params.text_color) rootStyle.setProperty('--tg-text', params.text_color);
  if (params.button_color) rootStyle.setProperty('--accent', params.button_color);
}

function render() {
  let inner;
  if (state.loading && !state.status) {
    inner = '<section class="launch-screen"><div class="orb"></div><h1>正在校验 Telegram 安全信号</h1><p>initData → HMAC → 管理员白名单</p></section>';
  } else if (state.route === 'dashboard') {
    inner = renderDashboard(state);
  } else if (state.route === 'studio') {
    inner = renderRuleStudio(state);
  } else if (state.route === 'rules') {
    inner = renderRules(state);
  } else if (state.route === 'logs') {
    inner = renderLogs(state);
  } else if (state.route === 'system') {
    inner = renderSystem(state);
  } else {
    inner = renderDashboard(state);
  }
  root.innerHTML = renderShell(inner, state);
  bindPage();
  if (tg) {
    if (state.route === 'dashboard') tg.BackButton.hide();
    else tg.BackButton.show();
  }
}

function bindPage() {
  root.querySelectorAll('[data-route]').forEach((button) => {
    button.addEventListener('click', () => navigate(button.dataset.route));
  });
  root.querySelectorAll('[data-action="pause"]').forEach((button) => {
    button.addEventListener('click', async () => {
      const paused = button.dataset.paused === 'true';
      await api.pause(paused);
      tg?.HapticFeedback?.notificationOccurred?.('success');
      await refreshStatus();
    });
  });
  root.querySelectorAll('[data-action="edit"]').forEach((button) => {
    button.addEventListener('click', () => {
      const rule = state.rules.find((item) => String(item.id) === String(button.dataset.id));
      navigate('studio', { rule });
    });
  });
  root.querySelectorAll('[data-action="duplicate"]').forEach((button) => {
    button.addEventListener('click', async () => {
      await api.duplicateRule(button.dataset.id, { enabled: false });
      tg?.HapticFeedback?.notificationOccurred?.('success');
      await refreshRules();
    });
  });
  root.querySelectorAll('[data-action="toggle"]').forEach((button) => {
    button.addEventListener('click', async () => {
      await api.setRuleEnabled(button.dataset.id, button.dataset.enabled === 'true');
      tg?.HapticFeedback?.impactOccurred?.('light');
      await refreshRules();
      await refreshStatus();
    });
  });
  root.querySelectorAll('[data-action="delete"]').forEach((button) => {
    button.addEventListener('click', async () => {
      const confirmed = tg?.showConfirm
        ? await new Promise((resolve) => tg.showConfirm('确认删除这条规则？', resolve))
        : window.confirm('确认删除这条规则？');
      if (!confirmed) return;
      await api.deleteRule(button.dataset.id);
      tg?.HapticFeedback?.notificationOccurred?.('success');
      await refreshRules();
      await refreshStatus();
    });
  });
  root.querySelectorAll('[data-action="logs-filter"]').forEach((button) => {
    button.addEventListener('click', async () => refreshLogs(button.dataset.status));
  });
  if (state.route === 'studio') bindRuleStudio(root, navigate);
  if (state.route === 'rules') filterRules();
}

subscribe(render);
initTelegramShell();
applyTelegramTheme();
bootstrapData();
setInterval(() => {
  if (state.route === 'dashboard' || state.route === 'system') {
    refreshStatus().catch((error) => setState({ error }));
  }
}, 15000);
