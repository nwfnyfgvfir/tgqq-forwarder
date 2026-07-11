export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

export function short(value, size = 18) {
  const text = String(value ?? '');
  if (text.length <= size) return text;
  return `${text.slice(0, Math.max(4, size - 7))}…${text.slice(-4)}`;
}

export function formatDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export function statusPill(label, state = 'neutral') {
  return `<span class="pill pill-${state}"><i></i>${escapeHtml(label)}</span>`;
}

export function statTile(label, value, hint, state = 'neutral') {
  return `
    <article class="stat-tile stat-${state}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <em>${escapeHtml(hint || '')}</em>
    </article>
  `;
}

export function pageHeader(kicker, title, body, actionHtml = '') {
  return `
    <header class="page-header">
      <div>
        <p class="kicker">${escapeHtml(kicker)}</p>
        <h1>${escapeHtml(title)}</h1>
        <p>${escapeHtml(body)}</p>
      </div>
      <div class="header-actions">${actionHtml}</div>
    </header>
  `;
}

export function emptyState(title, body, actionHtml = '') {
  return `
    <section class="empty-state">
      <div class="radar-dot"></div>
      <h3>${escapeHtml(title)}</h3>
      <p>${escapeHtml(body)}</p>
      ${actionHtml}
    </section>
  `;
}

export function navItem(id, label, icon, active) {
  return `<button class="nav-item ${active ? 'active' : ''}" data-route="${id}"><span>${icon}</span>${escapeHtml(label)}</button>`;
}

export function renderShell(inner, state) {
  const nav = [
    ['dashboard', '总览', '◉'],
    ['studio', '规则工坊', '✦'],
    ['rules', '规则库', '▤'],
    ['logs', '日志', '⌁'],
    ['system', '系统', '⌬'],
  ]
    .map(([id, label, icon]) => navItem(id, label, icon, state.route === id))
    .join('');
  const name = state.me?.user?.display_name || '管理员';
  return `
    <div class="app-frame">
      <aside class="side-rail">
        <div class="brand">
          <b>TGQQ</b>
          <span>Forwarder</span>
        </div>
        <nav>${nav}</nav>
        <footer>
          <span>操作者</span>
          <strong>${escapeHtml(name)}</strong>
        </footer>
      </aside>
      <main class="workspace">
        ${state.error ? errorBanner(state.error) : ''}
        ${inner}
      </main>
    </div>
  `;
}

export function errorBanner(error) {
  const message = error?.message || String(error || '未知错误');
  const code = error?.code ? ` · ${error.code}` : '';
  return `<div class="error-banner">请求失败${escapeHtml(code)}：${escapeHtml(message)}</div>`;
}

export function ruleCard(rule) {
  const keywords = rule.keywords?.length ? rule.keywords.map((item) => `<span>${escapeHtml(item)}</span>`).join('') : '<em>全量/正则</em>';
  const state = rule.enabled ? statusPill('启用', 'good') : statusPill('禁用', 'muted');
  return `
    <article class="rule-card" data-rule-id="${rule.id}">
      <div class="rule-card-top">
        <div>
          <small>#${rule.id} · P${rule.priority}</small>
          <h3>${escapeHtml(rule.name)}</h3>
        </div>
        ${state}
      </div>
      <div class="rule-route">
        <code>账号 ${escapeHtml(rule.source_account_id ?? '*')}</code>
        <code>TG ${escapeHtml(rule.source_chat_id ?? '*')}</code>
        <span>→</span>
        <code>${escapeHtml(rule.qq_target_type)}:${escapeHtml(short(rule.qq_target_id, 20))}</code>
      </div>
      <div class="chips">${keywords}</div>
      <div class="card-actions">
        <button data-action="edit" data-id="${rule.id}">编辑</button>
        <button data-action="duplicate" data-id="${rule.id}">复制</button>
        <button data-action="toggle" data-id="${rule.id}" data-enabled="${rule.enabled ? 'false' : 'true'}">${rule.enabled ? '禁用' : '启用'}</button>
        <button class="danger" data-action="delete" data-id="${rule.id}">删除</button>
      </div>
    </article>
  `;
}

export function logRow(log) {
  const state = log.status === 'success' ? 'good' : log.status === 'failed' ? 'bad' : 'warning';
  return `
    <article class="log-row">
      <div>${statusPill(log.status, state)}<strong>#${escapeHtml(log.id)}</strong></div>
      <p>账号 ${escapeHtml(log.tg_account_id ?? '-')} · TG ${escapeHtml(log.tg_chat_id ?? '-')} / ${escapeHtml(log.tg_message_id ?? '-')} → ${escapeHtml(log.qq_target_type)}:${escapeHtml(short(log.qq_target_id, 22))}</p>
      <small>${escapeHtml(formatDate(log.created_at))} · 规则 ${escapeHtml(log.rule_id ?? '-')}</small>
      ${log.error_message ? `<pre>${escapeHtml(log.error_message)}</pre>` : ''}
    </article>
  `;
}
