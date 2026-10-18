import { api } from '../api.js?v=20261018-tgtg';
import { emptyState, escapeHtml, formatDate, pageHeader, statusPill } from '../components.js?v=20261018-tgtg';
import { refreshSubscriptions, state } from '../state.js?v=20261018-tgtg';

const DEFAULT_TEMPLATE = '━━━━━━━━━━━━━━\n📡 {subscription_name}\n🕒 {published_at}\n\n# {title}\n\n{summary}\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n🌐 译文\n{translation}\n\n🔗 {link}';
const ui = {
  draft: null,
  original: null,
  editId: null,
  editorLoading: false,
  editorSeq: 0,
  advanced: false,
  preview: null,
  previewStale: false,
  formError: '',
  formBusy: '',
  dialogs: [],
  searchSeq: 0,
  history: null,
  historySeq: 0,
  busy: new Map(),
  notices: new Map(),
  notice: '',
};

function activeView(view) {
  return view.isConnected ? view : document.querySelector('#subscriptions-view');
}

function errorText(error) {
  if (Array.isArray(error?.detail)) {
    return error.detail.map((item) => `${(item.loc || []).filter((part) => part !== 'body').join('.')}: ${item.msg || '配置无效'}`).join('；');
  }
  return error?.message || '请求失败，请重试';
}

function safeLink(url, label = url) {
  try {
    const parsed = new URL(String(url));
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return `<a href="${escapeHtml(parsed.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label || url)}</a>`;
    }
  } catch { /* 无效链接只显示文本。 */ }
  return escapeHtml(label || url || '无链接');
}

function option(value, label, selected) {
  return `<option value="${escapeHtml(value)}" ${String(selected ?? '') === String(value) ? 'selected' : ''}>${escapeHtml(label)}</option>`;
}

function statusBar(current) {
  const status = current.rssStatus;
  const notices = [];
  if (current.rssStatusError) notices.push(`RSS 状态读取失败：${errorText(current.rssStatusError)}。${status ? '以下为上次读取的状态。' : '发布状态暂不可确认。'}`);
  if (status) {
    if (!status.enabled) notices.push('RSS 功能已关闭：自动抓取与发布不会运行；仍可管理订阅和预览，检查与重试不可用。');
    if (!status.publisher_configured) notices.push('发布 Bot 未配置：可保存订阅、预览和检查抓取，但不会发布到 Telegram。');
    if (status.publishing_paused) notices.push('发布已暂停：不会发送到 Telegram；检查仅提交抓取任务，不代表已发布。');
    if (!status.worker_running) notices.push('RSS 工作器未运行：已提交的检查与重试需等待工作器恢复。');
    if (!status.telegram_ready) notices.push('Telegram 监听尚未就绪：RSS 发布到频道后，TG → QQ 转发需等待监听账号连接。');
    if (status.last_worker_error) notices.push(`工作器错误：${status.last_worker_error}`);
  }
  return `<div class="rss-status-strip" role="status">
    <div class="rss-status-heading"><strong>RSS → Telegram → QQ</strong>${status ? statusPill(status.enabled && status.publisher_configured && status.worker_running && !status.publishing_paused ? '发布可用' : '需留意', status.enabled && status.publisher_configured && status.worker_running && !status.publishing_paused ? 'good' : 'warning') : statusPill(current.rssLoading ? '正在读取状态' : '状态未知', 'muted')}</div>
    ${notices.length ? `<ul>${notices.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : status ? '<p>订阅检查与发布正常运行；QQ 转发仍需配置对应频道规则。</p>' : ''}
    ${status ? `<p class="muted">订阅 ${escapeHtml(status.total_subscriptions ?? 0)} · 启用 ${escapeHtml(status.enabled_subscriptions ?? 0)} · 已发送 ${escapeHtml(status.sent_count ?? 0)} · 待发送 ${escapeHtml(status.pending_count ?? 0)} · 失败 ${escapeHtml(status.failed_count ?? 0)}</p>` : ''}
  </div>`;
}

function card(subscription) {
  const id = String(subscription.id);
  const busy = ui.busy.has(id);
  const notice = ui.notices.get(id);
  const checked = subscription.initialized ? '基线已建立' : '等待首次建立基线';
  return `<article class="rule-card rss-card" data-rss-id="${escapeHtml(id)}" aria-busy="${busy}">
    <div class="rule-card-top"><div><small>#${escapeHtml(id)} · ${checked}</small><h3>${escapeHtml(subscription.name)}</h3>${subscription.feed_title ? `<p class="muted">${escapeHtml(subscription.feed_title)}</p>` : ''}</div>${statusPill(subscription.enabled ? '启用' : '停用', subscription.enabled ? 'good' : 'muted')}</div>
    <div class="rss-feed-url">${safeLink(subscription.feed_url)}</div>
    <dl class="rss-meta">
      <div><dt>TG 目标</dt><dd><code>${escapeHtml(subscription.tg_chat_id)}</code></dd></div>
      <div><dt>检查间隔</dt><dd>${escapeHtml(subscription.interval_seconds)} 秒</dd></div>
      <div><dt>上次检查</dt><dd>${escapeHtml(formatDate(subscription.last_checked_at))}</dd></div>
      <div><dt>最近成功</dt><dd>${escapeHtml(formatDate(subscription.last_success_at))}</dd></div>
      <div><dt>下次检查</dt><dd>${escapeHtml(formatDate(subscription.next_check_at))}</dd></div>
      <div><dt>连续失败</dt><dd>${escapeHtml(subscription.failure_count ?? 0)}</dd></div>
    </dl>
    <div class="rss-counts"><span>已发送 <b>${escapeHtml(subscription.sent_count ?? 0)}</b></span><span>待发送 <b>${escapeHtml(subscription.pending_count ?? 0)}</b></span><span class="${subscription.failed_count ? 'rss-error-text' : ''}">失败 <b>${escapeHtml(subscription.failed_count ?? 0)}</b></span></div>
    ${subscription.last_error ? `<p class="rss-error-text rss-last-error">最近错误：${escapeHtml(subscription.last_error)}</p>` : ''}
    <div class="card-actions">
      ${[['edit', '编辑'], ['toggle', subscription.enabled ? '停用' : '启用'], ['check', '立即检查'], ['test', '立即试发'], ...(subscription.failed_count > 0 ? [['retry', '重试失败']] : []), ['history', '条目历史'], ['rule', '配置QQ转发规则'], ['delete', '删除']].map(([action, label]) => `<button type="button" ${busy ? 'disabled' : ''} class="${action === 'delete' ? 'danger' : ''}" data-rss-action="${action}" data-id="${escapeHtml(id)}">${label}</button>`).join('')}
    </div>
    <p class="rss-card-notice ${notice?.error ? 'rss-error-text' : 'muted'}" role="status" data-rss-notice>${escapeHtml(busy ? ui.busy.get(id) : notice?.text || '')}</p>
  </article>`;
}

function list(current) {
  return `${current.rssSubscriptionsError ? `<div class="error-banner" role="alert">订阅读取失败：${escapeHtml(errorText(current.rssSubscriptionsError))}${current.rssSubscriptions.length ? '；保留上次列表。' : ''}</div>` : ''}
    ${current.rssSubscriptions.length ? `<div class="rss-list">${current.rssSubscriptions.map(card).join('')}</div>` : current.rssLoading ? '<div class="panel" role="status">正在加载 RSS 订阅…</div>' : current.rssSubscriptionsError ? '<p class="muted">请点击「刷新列表」重试，不影响其他面板功能。</p>' : emptyState('还没有 RSS 订阅', '先添加 RSS 源，将新条目发布到 Telegram 频道，再配置频道 → QQ 的转发规则。首次默认只建立基线，不发送旧文章。', '<button type="button" class="primary" data-rss-action="new">添加第一个订阅</button>')}`;
}

function formDefaults(subscription = {}) {
  return {
    name: subscription.name ?? '',
    feed_url: subscription.feed_url ?? '',
    tg_chat_id: String(subscription.tg_chat_id ?? ''),
    enabled: subscription.enabled ?? true,
    interval_seconds: String(subscription.interval_seconds ?? 600),
    initial_policy: subscription.initial_policy ?? 'skip',
    initial_count: String(subscription.initial_count ?? 3),
    include_keywords: (subscription.include_keywords || []).join(', '),
    exclude_keywords: (subscription.exclude_keywords || []).join(', '),
    message_template: subscription.message_template ?? DEFAULT_TEMPLATE,
    max_summary_length: String(subscription.max_summary_length ?? 600),
    translate: subscription.translate ?? true,
    listener_account: '',
    dialog_query: '',
  };
}

function previewHtml() {
  if (!ui.preview) return '<p class="muted">使用当前完整配置抓取并渲染预览，不保存订阅、不建立基线，也不发送消息。</p>';
  return `<p class="muted">${ui.previewStale ? '配置已变化，请重新预览。以下为上一次结果。' : '仅预览：没有保存或发送。'}</p>
    <h3>${escapeHtml(ui.preview.feed_title || '未命名订阅源')}</h3>
    ${(ui.preview.warnings || []).length ? `<ul class="rss-warnings">${ui.preview.warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join('')}</ul>` : ''}
    ${(ui.preview.entries || []).length ? ui.preview.entries.map((entry) => `<article class="rss-preview-entry"><div class="rss-entry-heading"><strong>${escapeHtml(entry.title || '无标题')}</strong>${statusPill(entry.matches ? '匹配' : '不匹配', entry.matches ? 'good' : 'muted')}</div><p>${safeLink(entry.link)} · ${escapeHtml(formatDate(entry.published_at))}</p>${entry.summary ? `<p class="rss-summary">${escapeHtml(entry.summary)}</p>` : ''}${entry.translated_title || entry.translated_summary ? `<div class="rss-translation"><p class="muted">译文</p>${entry.translated_title ? `<p><strong>${escapeHtml(entry.translated_title)}</strong></p>` : ''}${entry.translated_summary ? `<p class="rss-summary">${escapeHtml(entry.translated_summary)}</p>` : ''}</div>` : ''}<pre>${escapeHtml(entry.rendered_text || '')}</pre></article>`).join('') : '<p class="muted">该订阅源当前没有条目。</p>'}`;
}

function editor(current) {
  if (ui.editorLoading) return '<section class="panel" role="status">正在读取完整订阅配置… <button type="button" data-rss-action="cancel">取消</button></section>';
  if (!ui.draft) return '';
  const draft = ui.draft;
  return `<section class="panel rss-editor" aria-labelledby="rss-editor-title">
    <div class="panel-head"><h2 id="rss-editor-title">${ui.editId === null ? '新建订阅' : `编辑订阅 #${escapeHtml(ui.editId)}`}</h2><button type="button" data-rss-action="cancel" ${ui.formBusy ? 'disabled' : ''}>取消</button></div>
    <form id="rss-form" novalidate aria-busy="${Boolean(ui.formBusy)}">
      <fieldset ${ui.formBusy ? 'disabled' : ''}>
        <div class="grid-2">
          <label for="rss-name">订阅名称（必填）<input id="rss-name" name="name" required minlength="1" maxlength="120" value="${escapeHtml(draft.name)}" /></label>
          <label for="rss-feed-url">Feed URL（必填）<input id="rss-feed-url" name="feed_url" type="url" required value="${escapeHtml(draft.feed_url)}" aria-describedby="rss-feed-help rss-feed-change" placeholder="输入 http(s) RSS 地址" /></label>
        </div>
        <p id="rss-feed-help" class="muted">仅支持 http / https。订阅源内容以文本展示，不执行 Feed HTML。</p>
        <p id="rss-feed-change" class="rss-warnings" hidden>修改 Feed URL 会重建基线，旧的未完成条目将跳过。</p>
        <label for="rss-chat-id">Telegram 目标（必填）<input id="rss-chat-id" name="tg_chat_id" required value="${escapeHtml(draft.tg_chat_id)}" aria-describedby="rss-target-help rss-target-change" placeholder="负整数频道 ID 或 @username" /></label>
        <p id="rss-target-help" class="muted">建议用数字频道 ID，便于配置 TG → QQ 规则；@username 不能直接作为规则的 source_chat_id。</p>
        <p id="rss-target-change" class="rss-warnings" hidden>修改目标后，待发送条目会走新目标；已发布条目不会重发。</p>
        <details class="rss-disclosure"><summary>从监听账号搜索频道</summary>
          <p class="muted">搜索的是监听账号可见频道，不是发布 Bot 的频道列表；选择后填入数字 ID。</p>
          <div class="grid-2"><label for="rss-listener-account">监听账号<select id="rss-listener-account" name="listener_account">${option('', '任意监听账号', draft.listener_account)}${(current.options?.telegram_account_ids || []).map((account) => option(account, account, draft.listener_account)).join('')}</select></label><label for="rss-dialog-query">频道名称或 ID<input id="rss-dialog-query" name="dialog_query" value="${escapeHtml(draft.dialog_query)}" /></label></div>
          <button type="button" data-rss-action="search">搜索频道</button><div id="rss-dialog-results" class="choice-list" role="status"></div>
        </details>
        <label class="switch-line" for="rss-enabled"><input id="rss-enabled" name="enabled" type="checkbox" ${draft.enabled ? 'checked' : ''} /> 保存后启用订阅</label>
        <p id="rss-initial-help" class="muted"></p>
        <details id="rss-advanced" class="rss-disclosure" ${ui.advanced ? 'open' : ''}><summary>检查频率、首次策略、过滤与消息模板</summary>
          <div class="grid-2"><label for="rss-interval">检查间隔（秒）<input id="rss-interval" name="interval_seconds" type="number" required min="60" max="86400" step="1" value="${escapeHtml(draft.interval_seconds)}" /></label><label for="rss-initial-policy">首次处理策略<select id="rss-initial-policy" name="initial_policy">${option('skip', '只建立基线（默认）', draft.initial_policy)}${option('latest', '发送最近 N 条', draft.initial_policy)}</select></label></div>
          <label id="rss-initial-count-label" for="rss-initial-count">首次发送数量（1–20 条）<input id="rss-initial-count" name="initial_count" type="number" required min="1" max="20" step="1" value="${escapeHtml(draft.initial_count)}" /></label>
          <div class="grid-2"><label for="rss-include">包含关键词<textarea id="rss-include" name="include_keywords" rows="2">${escapeHtml(draft.include_keywords)}</textarea></label><label for="rss-exclude">排除关键词<textarea id="rss-exclude" name="exclude_keywords" rows="2">${escapeHtml(draft.exclude_keywords)}</textarea></label></div>
          <p class="muted">关键词用逗号、中文逗号、分号或换行分隔；包含列表留空则不过滤包含关键词。</p>
          <label for="rss-summary-length">摘要最大长度（字符）<input id="rss-summary-length" name="max_summary_length" type="number" required min="100" max="2000" step="1" value="${escapeHtml(draft.max_summary_length)}" /></label>
          <label class="switch-line" for="rss-translate"><input id="rss-translate" name="translate" type="checkbox" ${draft.translate ? 'checked' : ''} /> 翻译成中文（源语言自动检测，非中文才翻译；译文显示在原文下方）</label>
          <label for="rss-template">消息模板<textarea id="rss-template" name="message_template" required rows="10">${escapeHtml(draft.message_template)}</textarea></label>
          <p class="muted">默认变量：<code>{subscription_name}</code>、<code>{title}</code>、<code>{published_at}</code>、<code>{summary}</code>、<code>{translation}</code>、<code>{link}</code>。<code>{published_at}</code> 渲染为本地时间（如 2026-10-13 08:30）。某一行引用的占位符全为空时整行省略，空行分隔的整段全为空时整段省略——所以可以放心给可选字段加 <code>🕒</code> 这类前缀，源里没时间、没译文时不会留下孤立图标。<code># {title}</code> 里的 <code>#</code> 在转发到 QQ 时会渲染成一号标题（<code>##</code> 为二号），在 Telegram 里按原样显示。<code>{translation}</code> 为译文，只包含模板里写到的原文字段（没写 <code>{title}</code> 就不翻译标题），会排在原文下方。QQ 转发规则使用 <code>{text}</code> 保留发布内容。</p>
        </details>
        <p id="rss-form-error" class="rss-error-text" role="alert">${escapeHtml(ui.formError)}</p>
        <div class="card-actions"><button type="submit" class="primary">${ui.formBusy === 'save' ? '正在保存…' : ui.editId === null ? '创建订阅' : '保存配置'}</button><button type="button" data-rss-action="preview">${ui.formBusy === 'preview' ? '正在预览…' : '预览（不保存、不发送）'}</button></div>
      </fieldset>
    </form>
    <section class="rss-preview" aria-labelledby="rss-preview-title"><h2 id="rss-preview-title">抓取与消息预览</h2><div id="rss-preview-result" aria-live="polite">${previewHtml()}</div></section>
  </section>`;
}

function historyHtml() {
  const history = ui.history;
  if (!history) return '';
  const labels = { pending: ['待发送', 'warning'], sending: ['发送中', 'warning'], sent: ['已发送', 'good'], failed: ['失败', 'bad'], skipped: ['已跳过', 'muted'] };
  return `<section class="panel rss-history" aria-labelledby="rss-history-title"><div class="panel-head"><h2 id="rss-history-title">条目历史 · ${escapeHtml(history.name)}</h2><div class="card-actions"><button type="button" data-rss-action="refresh-history" ${history.loading ? 'disabled' : ''}>刷新历史</button><button type="button" data-rss-action="close-history">关闭</button></div></div><p class="muted">最近 50 条 · 手动刷新，不打断阅读。</p>${history.loading ? '<p role="status">正在读取条目…</p>' : ''}${history.error ? `<p class="rss-error-text" role="alert">${escapeHtml(history.error)}</p>` : ''}
    ${(history.entries || []).map((entry) => { const [label, tone] = labels[entry.status] || [entry.status, 'neutral']; return `<article class="rss-history-entry"><div class="rss-entry-heading"><h3>${escapeHtml(entry.title || '无标题')}</h3>${statusPill(label, tone)}</div><p>${safeLink(entry.link)}</p><p class="muted">#${escapeHtml(entry.id)} · 发布 ${escapeHtml(formatDate(entry.published_at))} · 尝试 ${escapeHtml(entry.attempts ?? 0)} 次 · 创建 ${escapeHtml(formatDate(entry.created_at))} · 发送 ${escapeHtml(formatDate(entry.sent_at))} · TG 消息 ${escapeHtml(entry.tg_message_id ?? '-')}</p>${entry.last_error ? `<pre class="rss-error-text">${escapeHtml(entry.last_error)}</pre>` : ''}</article>`; }).join('')}
    ${!history.loading && !history.error && !history.entries?.length ? '<p class="muted">暂无条目；首次 skip 只建立基线，旧文章不会发送。</p>' : ''}
  </section>`;
}

export function renderSubscriptions(current) {
  return `<div id="subscriptions-view" class="subscriptions-view">
    ${pageHeader('RSS SUBSCRIPTIONS', '统一订阅管理', 'RSS 抓取与 Telegram 发布在这里管理；QQ 转发继续使用现有规则。', '<button type="button" data-rss-action="refresh">刷新列表</button><button type="button" class="primary" data-rss-action="new">新增订阅</button>')}
    <div id="rss-status">${statusBar(current)}</div>
    <details class="rss-disclosure rss-setup"><summary>发布与 QQ 转发的配置说明</summary><ul><li><code>RSS_BOT_TOKEN</code> 可选，未单独配置时默认使用管理 Bot；未配置任何可用 Bot 时仍可 CRUD 和预览。</li><li>发布 Bot 需成为目标频道管理员并具备发消息权限；监听账号也需加入该频道。</li><li>QQ 规则按频道数字 ID 匹配，发送者 Bot 标记保持「任意」，不要限制为真人；然后手动选择 QQ 目标。</li><li>「立即检查」与「重试」仅提交异步任务，不代表已经抓取或发送完成。</li></ul></details>
    <p id="rss-page-notice" class="muted" role="status">${escapeHtml(ui.notice)}</p>
    <div id="rss-editor-slot">${editor(current)}</div>
    <div id="rss-history-slot">${historyHtml()}</div>
    <div id="rss-list-slot">${list(current)}</div>
  </div>`;
}

export function canRefreshSubscriptions(root) {
  const view = root.matches?.('#subscriptions-view') ? root : root.querySelector('#subscriptions-view');
  return Boolean(view && !ui.draft && !ui.editorLoading && !ui.history && !ui.busy.size && !view.contains(document.activeElement));
}

export function updateSubscriptions(root, current) {
  const view = root.matches?.('#subscriptions-view') ? root : root.querySelector('#subscriptions-view');
  if (!view) return;
  view.querySelector('#rss-status').innerHTML = statusBar(current);
  const refresh = view.querySelector('[data-rss-action="refresh"]');
  refresh.disabled = current.rssLoading;
  refresh.textContent = current.rssLoading ? '正在刷新…' : '刷新列表';
  const slot = view.querySelector('#rss-list-slot');
  if (!ui.busy.size) {
    if (!slot.contains(document.activeElement)) slot.innerHTML = list(current);
    else updateFocusedCard(view, current);
  }
  if (ui.draft && !current.rssLoading) {
    const accounts = view.querySelector('#rss-listener-account');
    if (accounts && accounts !== document.activeElement) {
      accounts.innerHTML = option('', '任意监听账号', ui.draft.listener_account) + (current.options?.telegram_account_ids || []).map((account) => option(account, account, ui.draft.listener_account)).join('');
    }
  }
}

function updateFocusedCard(view, current) {
  const focusedCard = document.activeElement?.closest?.('[data-rss-id]');
  if (!focusedCard || !view.contains(focusedCard)) return;
  const subscription = current.rssSubscriptions.find((item) => String(item.id) === focusedCard.dataset.rssId);
  if (!subscription) {
    view.querySelector('[data-rss-action="new"]').focus();
    view.querySelector('#rss-list-slot').innerHTML = list(current);
    return;
  }
  const template = document.createElement('template');
  template.innerHTML = card(subscription);
  const fresh = template.content.firstElementChild;
  focusedCard.setAttribute('aria-busy', 'false');
  for (const selector of ['.rule-card-top', '.rss-feed-url', '.rss-meta', '.rss-counts', '[data-rss-notice]']) {
    const node = focusedCard.querySelector(selector);
    if (!node.contains(document.activeElement)) node.replaceWith(fresh.querySelector(selector).cloneNode(true));
  }
  const lastError = focusedCard.querySelector('.rss-last-error');
  const newError = fresh.querySelector('.rss-last-error');
  if (lastError) {
    if (newError) lastError.replaceWith(newError.cloneNode(true));
    else lastError.remove();
  } else if (newError) focusedCard.querySelector('.card-actions').before(newError.cloneNode(true));
  const toggle = focusedCard.querySelector('[data-rss-action="toggle"]');
  if (toggle) toggle.textContent = subscription.enabled ? '停用' : '启用';
  const retry = focusedCard.querySelector('[data-rss-action="retry"]');
  if (subscription.failed_count > 0 && !retry) {
    focusedCard.querySelector('[data-rss-action="history"]').before(fresh.querySelector('[data-rss-action="retry"]').cloneNode(true));
  } else if (retry) retry.hidden = !(subscription.failed_count > 0) && retry !== document.activeElement;
}

function syncDraft(form) {
  if (!ui.draft) return;
  const checkboxes = ['enabled', 'translate'];
  Object.keys(ui.draft).forEach((name) => {
    const control = form.elements.namedItem(name);
    if (control) ui.draft[name] = checkboxes.includes(name) ? control.checked : control.value;
  });
}

function updateFormHints(form) {
  if (!ui.draft) return;
  const latest = ui.draft.initial_policy === 'latest';
  form.querySelector('#rss-initial-count-label').hidden = !latest;
  form.elements.initial_count.disabled = !latest || Boolean(ui.formBusy);
  form.querySelector('#rss-initial-help').textContent = latest ? '首次建立基线时，将最近 N 条加入发送队列；后续只处理新条目。' : '首次只建立去重基线，不发送已有文章；之后检查发现的新条目才进入发送队列。';
  form.querySelector('#rss-feed-change').hidden = !ui.original || ui.draft.feed_url.trim() === ui.original.feed_url;
  form.querySelector('#rss-target-change').hidden = !ui.original || ui.draft.tg_chat_id.trim() === String(ui.original.tg_chat_id);
}

function formPayload(form) {
  syncDraft(form);
  const draft = ui.draft;
  const name = form.elements.name;
  name.setCustomValidity(draft.name.trim() ? '' : '请输入订阅名称');
  const feed = form.elements.feed_url;
  let validFeed = false;
  try { validFeed = ['http:', 'https:'].includes(new URL(draft.feed_url.trim()).protocol); } catch { /* 由原生校验提示。 */ }
  feed.setCustomValidity(validFeed ? '' : '请输入有效的 http 或 https Feed URL');
  const chat = draft.tg_chat_id.trim();
  const numericChat = /^-[1-9]\d*$/.test(chat) && Number.isSafeInteger(Number(chat));
  form.elements.tg_chat_id.setCustomValidity(numericChat || /^@[A-Za-z][A-Za-z0-9_]{4,31}$/.test(chat) ? '' : '请输入负整数频道 ID 或有效的 @username');
  if (!form.checkValidity()) {
    form.querySelector('#rss-advanced').open = true;
    form.reportValidity();
    return null;
  }
  const keywords = (text) => text.split(/[,，;；\r\n]+/).map((item) => item.trim()).filter(Boolean);
  return {
    name: draft.name.trim(), feed_url: draft.feed_url.trim(), tg_chat_id: chat,
    enabled: draft.enabled, interval_seconds: Number(draft.interval_seconds), initial_policy: draft.initial_policy,
    initial_count: draft.initial_policy === 'latest' || (Number.isInteger(Number(draft.initial_count)) && Number(draft.initial_count) >= 1 && Number(draft.initial_count) <= 20) ? Number(draft.initial_count) : 3,
    include_keywords: keywords(draft.include_keywords), exclude_keywords: keywords(draft.exclude_keywords),
    message_template: draft.message_template, max_summary_length: Number(draft.max_summary_length),
    translate: Boolean(draft.translate),
  };
}

function paintEditor(view) {
  view.querySelector('#rss-editor-slot').innerHTML = editor(state);
  const form = view.querySelector('#rss-form');
  if (form) {
    updateFormHints(form);
    bindForm(form, view);
  }
}

function setFormBusy(view, action) {
  ui.formBusy = action;
  const form = view.querySelector('#rss-form');
  if (!form) return;
  form.setAttribute('aria-busy', String(Boolean(action)));
  form.querySelector('fieldset').disabled = Boolean(action);
  view.querySelector('[data-rss-action="cancel"]').disabled = Boolean(action);
  form.querySelector('[type="submit"]').textContent = action === 'save' ? '正在保存…' : ui.editId === null ? '创建订阅' : '保存配置';
  form.querySelector('[data-rss-action="preview"]').textContent = action === 'preview' ? '正在预览…' : '预览（不保存、不发送）';
  updateFormHints(form);
}

function closeEditor(view) {
  ui.editorSeq += 1;
  ui.searchSeq += 1;
  ui.draft = null;
  ui.original = null;
  ui.editorLoading = false;
  ui.formBusy = '';
  ui.preview = null;
  ui.formError = '';
  paintEditor(view);
}

function bindForm(form, view) {
  const onChange = () => {
    syncDraft(form);
    ['name', 'feed_url', 'tg_chat_id'].forEach((name) => form.elements[name].setCustomValidity(''));
    updateFormHints(form);
    if (ui.preview && !ui.previewStale) {
      ui.previewStale = true;
      view.querySelector('#rss-preview-result').innerHTML = previewHtml();
    }
  };
  form.addEventListener('input', onChange);
  form.addEventListener('change', (event) => {
    onChange();
    if (event.target.name === 'listener_account' || event.target.name === 'dialog_query') {
      ui.searchSeq += 1;
      ui.dialogs = [];
      form.querySelector('#rss-dialog-results').replaceChildren();
    }
  });
  form.querySelector('#rss-advanced').addEventListener('toggle', (event) => { ui.advanced = event.target.open; });
  form.addEventListener('submit', (event) => { event.preventDefault(); submitForm(view, false); });
}

async function submitForm(view, previewOnly) {
  if (ui.formBusy) return;
  const form = view.querySelector('#rss-form');
  const payload = formPayload(form);
  if (!payload) return;
  const seq = ui.editorSeq;
  ui.formError = '';
  form.querySelector('#rss-form-error').textContent = '';
  setFormBusy(view, previewOnly ? 'preview' : 'save');
  try {
    const result = previewOnly ? await api.previewRss(payload) : ui.editId === null ? await api.createRssSubscription(payload) : await api.updateRssSubscription(ui.editId, payload);
    if (seq !== ui.editorSeq) return;
    const currentView = activeView(view);
    if (previewOnly) {
      ui.preview = result;
      ui.previewStale = false;
      if (currentView) currentView.querySelector('#rss-preview-result').innerHTML = previewHtml();
    } else {
      ui.notice = `已保存订阅「${result.name || payload.name}」。${!ui.original?.initialized || ui.original.feed_url !== payload.feed_url ? payload.initial_policy === 'skip' ? '首次只建立基线，不发送旧文章。' : '首次将最近条目加入发送队列。' : '已发布条目不会重发。'}`;
      if (currentView) {
        closeEditor(currentView);
        currentView.querySelector('#rss-page-notice').textContent = ui.notice;
        currentView.querySelector('[data-rss-action="new"]').focus();
      } else {
        ui.editorSeq += 1;
        ui.draft = null;
        ui.original = null;
        ui.preview = null;
        ui.formBusy = '';
      }
      await refreshSubscriptions();
    }
  } catch (error) {
    if (seq !== ui.editorSeq) return;
    ui.formError = `${previewOnly ? '预览' : '保存'}失败：${errorText(error)}`;
    activeView(view)?.querySelector('#rss-form-error')?.replaceChildren(document.createTextNode(ui.formError));
  } finally {
    if (seq === ui.editorSeq) setFormBusy(activeView(view) || view, '');
  }
}

async function searchDialogs(view) {
  const form = view.querySelector('#rss-form');
  syncDraft(form);
  const seq = ++ui.searchSeq;
  const container = form.querySelector('#rss-dialog-results');
  const button = form.querySelector('[data-rss-action="search"]');
  button.disabled = true;
  button.textContent = '正在搜索…';
  container.textContent = '正在搜索监听账号可见频道…';
  try {
    const items = await api.dialogs(ui.draft.dialog_query.trim(), 80, ui.draft.listener_account);
    if (seq !== ui.searchSeq || !view.isConnected) return;
    ui.dialogs = items.filter((item) => item.type === 'channel');
    container.innerHTML = ui.dialogs.length ? ui.dialogs.map((item, index) => `<button type="button" class="choice-card" data-rss-action="choose-dialog" data-index="${index}"><strong>${escapeHtml(item.name)}</strong><code>${escapeHtml(item.id)}</code><span>${escapeHtml(item.account_id || '频道')}</span></button>`).join('') : '<p class="muted">没有匹配频道。请确认监听账号已加入，或更换账号搜索。</p>';
  } catch (error) {
    if (seq === ui.searchSeq && view.isConnected) container.textContent = `搜索失败：${errorText(error)}`;
  } finally {
    button.disabled = Boolean(ui.formBusy);
    button.textContent = '搜索频道';
  }
}

function paintHistory(view) {
  view.querySelector('#rss-history-slot').innerHTML = historyHtml();
}

async function loadHistory(view) {
  const history = ui.history;
  if (!history || history.loading) return;
  const seq = ++ui.historySeq;
  history.loading = true;
  history.error = '';
  paintHistory(view);
  try {
    const entries = await api.rssEntries(history.id, 50);
    if (seq === ui.historySeq) history.entries = entries;
  } catch (error) {
    if (seq === ui.historySeq) history.error = `历史读取失败：${errorText(error)}`;
  } finally {
    if (seq === ui.historySeq) {
      history.loading = false;
      const currentView = activeView(view);
      if (currentView) paintHistory(currentView);
    }
  }
}

function paintCard(view, id) {
  const node = Array.from(view.querySelectorAll('[data-rss-id]')).find((item) => item.dataset.rssId === id);
  if (!node) return;
  const busy = ui.busy.has(id);
  node.setAttribute('aria-busy', String(busy));
  node.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
  const notice = node.querySelector('[data-rss-notice]');
  notice.textContent = busy ? ui.busy.get(id) : ui.notices.get(id)?.text || '';
  notice.classList.toggle('rss-error-text', Boolean(ui.notices.get(id)?.error));
}

async function cardAction(view, subscription, action, navigate) {
  const id = String(subscription.id);
  if (ui.busy.has(id)) return;
  if (action === 'rule') {
    const chat = String(subscription.tg_chat_id);
    if (!/^-[1-9]\d*$/.test(chat) || !Number.isSafeInteger(Number(chat))) {
      ui.notices.set(id, { error: true, text: '@username 不能作为 source_chat_id。请编辑订阅，从监听账号搜索频道并选择数字 ID，再配置 QQ 转发规则。' });
      paintCard(view, id);
      return;
    }
    navigate('studio', { rule: { source_chat_id: Number(chat), source_chat_type: 'channel', source_sender_is_bot: null, match_mode: 'all', message_template: '{text}', name: `RSS ${subscription.name}`.slice(0, 120) } });
    return;
  }
  if (action === 'history') {
    ui.historySeq += 1;
    ui.history = { id, name: subscription.name, entries: [], loading: false, error: '' };
    await loadHistory(view);
    if (view.isConnected) view.querySelector('#rss-history-title')?.scrollIntoView({ block: 'nearest' });
    return;
  }
  if (action === 'edit') {
    if (ui.draft || ui.editorLoading) { view.querySelector('#rss-editor-slot').scrollIntoView({ block: 'start' }); return; }
    const seq = ++ui.editorSeq;
    ui.editorLoading = true;
    ui.editId = id;
    paintEditor(view);
    try {
      const full = await api.rssSubscription(id);
      if (seq !== ui.editorSeq) return;
      ui.original = full;
      ui.draft = formDefaults(full);
      ui.advanced = false;
      ui.preview = null;
      ui.previewStale = false;
      ui.formError = '';
    } catch (error) {
      if (seq === ui.editorSeq) ui.notices.set(id, { error: true, text: `读取配置失败：${errorText(error)}` });
    } finally {
      if (seq === ui.editorSeq) {
        ui.editorLoading = false;
        const currentView = activeView(view);
        if (currentView) { paintEditor(currentView); paintCard(currentView, id); currentView.querySelector('#rss-name')?.focus(); }
      }
    }
    return;
  }
  ui.busy.set(id, action === 'delete' ? '等待删除确认…' : '正在提交…');
  ui.notices.delete(id);
  paintCard(view, id);
  try {
    if (action === 'delete') {
      const message = `确认删除订阅「${subscription.name}」？这会删除全部条目和去重数据；重建订阅后可能重复发送。`;
      const tg = window.Telegram?.WebApp;
      const confirmed = tg?.showConfirm ? await new Promise((resolve) => tg.showConfirm(message, resolve)) : window.confirm(message);
      if (!confirmed) return;
      await api.deleteRssSubscription(id);
      ui.notice = `已删除订阅「${subscription.name}」及其条目、去重数据。`;
      const currentView = activeView(view);
      if (String(ui.editId) === id) {
        if (currentView) closeEditor(currentView);
        else { ui.editorSeq += 1; ui.draft = null; ui.original = null; ui.preview = null; ui.formBusy = ''; }
      }
      if (ui.history?.id === id) { ui.historySeq += 1; ui.history = null; if (currentView) paintHistory(currentView); }
      if (currentView) currentView.querySelector('#rss-page-notice').textContent = ui.notice;
    } else if (action === 'toggle') {
      await api.setRssEnabled(id, !subscription.enabled);
      ui.notices.set(id, { text: subscription.enabled ? '订阅已停用。' : '订阅已启用。' });
    } else if (action === 'check') {
      const result = await api.checkRssSubscription(id);
      ui.notices.set(id, { text: `${result.scheduled ? '检查已排队' : '检查请求已提交'}，稍后刷新查看结果。${!state.rssStatus?.publisher_configured || state.rssStatus?.publishing_paused || !state.rssStatus?.enabled ? '当前发布不可用或状态未知，抓取检查不代表会发布。' : ''}` });
    } else if (action === 'test') {
      ui.busy.set(id, '正在抓取并试发最新一条…');
      paintCard(view, id);
      const result = await api.testSendRssSubscription(id);
      const bits = [];
      if (result.sent) {
        bits.push(`已试发最新一条到频道（消息 #${result.message_id}）：${result.entry?.title || '无标题'}。`);
        bits.push(result.matched ? '命中关键词。' : '未命中关键词（本次强制发送，正式发布会被跳过）。');
        bits.push(result.translated ? '已附加译文。' : '未附加译文。');
      } else {
        bits.push(`试发失败：${result.error || '未知错误'}`);
      }
      for (const warning of result.warnings || []) bits.push(warning);
      bits.push('请到目标频道确认，并检查 TG → QQ 是否已转发。');
      ui.notices.set(id, { error: !result.sent, text: bits.join(' ') });
    } else if (action === 'retry') {
      const result = await api.retryRssSubscription(id);
      ui.notices.set(id, { text: `重试已排队，共 ${result.retried_count ?? 0} 条；不代表已发送成功。` });
    }
    await refreshSubscriptions();
  } catch (error) {
    ui.notices.set(id, { error: true, text: `操作失败：${errorText(error)}` });
  } finally {
    ui.busy.delete(id);
    const currentView = activeView(view);
    if (currentView) { updateSubscriptions(currentView, state); paintCard(currentView, id); }
  }
}

export function bindSubscriptions(root, navigate) {
  const view = root.matches?.('#subscriptions-view') ? root : root.querySelector('#subscriptions-view');
  if (!view) return;
  const form = view.querySelector('#rss-form');
  if (form) { updateFormHints(form); bindForm(form, view); }
  updateSubscriptions(root, state);
  view.addEventListener('focusout', (event) => {
    const slot = view.querySelector('#rss-list-slot');
    if (!slot.contains(event.target)) return;
    queueMicrotask(() => {
      if (!view.isConnected || ui.busy.size) return;
      const slot = view.querySelector('#rss-list-slot');
      if (!slot.contains(document.activeElement)) slot.innerHTML = list(state);
    });
  });
  view.addEventListener('click', async (event) => {
    const button = event.target instanceof Element ? event.target.closest('[data-rss-action]') : null;
    if (!button || !view.contains(button) || button.disabled) return;
    const action = button.dataset.rssAction;
    if (action === 'new') {
      if (!ui.draft && !ui.editorLoading) {
        ui.editorSeq += 1;
        ui.editId = null;
        ui.original = null;
        ui.draft = formDefaults();
        ui.preview = null;
        ui.previewStale = false;
        ui.formError = '';
        ui.advanced = false;
        paintEditor(view);
      }
      view.querySelector('#rss-name')?.focus();
    } else if (action === 'cancel') {
      if (!ui.formBusy) { closeEditor(view); view.querySelector('[data-rss-action="new"]').focus(); }
    } else if (action === 'refresh') {
      await refreshSubscriptions();
    } else if (action === 'preview') {
      await submitForm(view, true);
    } else if (action === 'search') {
      await searchDialogs(view);
    } else if (action === 'choose-dialog') {
      const item = ui.dialogs[Number(button.dataset.index)];
      if (!item || !ui.draft || ui.formBusy) return;
      const currentForm = view.querySelector('#rss-form');
      currentForm.elements.tg_chat_id.value = String(item.id);
      currentForm.elements.tg_chat_id.dispatchEvent(new Event('input', { bubbles: true }));
      currentForm.elements.tg_chat_id.focus();
    } else if (action === 'close-history') {
      ui.historySeq += 1;
      ui.history = null;
      paintHistory(view);
      view.querySelector('[data-rss-action="refresh"]').focus();
    } else if (action === 'refresh-history') {
      await loadHistory(view);
    } else {
      const subscription = state.rssSubscriptions.find((item) => String(item.id) === button.dataset.id);
      if (subscription) await cardAction(view, subscription, action, navigate);
    }
  });
  if (!state.rssLoading && !ui.draft && !ui.editorLoading) refreshSubscriptions();
}
