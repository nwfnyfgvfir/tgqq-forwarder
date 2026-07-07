import { api } from '../api.js';
import { escapeHtml, pageHeader, short, statusPill } from '../components.js';
import { refreshRules } from '../state.js';

const DEFAULT_TEMPLATE = '[Telegram: {chat_title}]\n{sender_name}: {text}\n{links_note}\n{media_note}';

export function renderRuleStudio(state) {
  const editRule = state.routeParams?.rule || null;
  return `
    ${pageHeader(
      'RULE STUDIO',
      editRule ? `编辑规则 #${editRule.id}` : '规则工坊',
      '用选择器、关键词芯片、模板预览完成高级规则配置，不再手写长命令。'
    )}
    <form id="rule-form" class="studio-layout" data-edit-id="${editRule?.id || ''}">
      <section class="panel studio-main">
        <div class="step-block">
          <div class="step-index">01</div>
          <div>
            <h2>Telegram 来源</h2>
            <p>选择监听账号可见的频道、群组或私聊；留空代表全部来源。</p>
          </div>
          <label>规则名称<input name="name" required maxlength="120" value="${escapeHtml(editRule?.name || '')}" placeholder="例如：LINUX DO AI 关键词" /></label>
          <div class="picker-row">
            <input id="dialog-query" placeholder="搜索 Telegram 会话名或 ID" />
            <button type="button" data-action="search-dialogs">搜索会话</button>
          </div>
          <div class="choice-list" id="dialog-results" hidden></div>
          <div class="grid-2">
            <label>TG 会话 ID<input name="source_chat_id" value="${escapeHtml(editRule?.source_chat_id ?? '')}" placeholder="* 或 -100..." /></label>
            <label>会话类型<select name="source_chat_type">
              ${option('', '任意', editRule?.source_chat_type)}
              ${option('channel', '频道', editRule?.source_chat_type)}
              ${option('group', '群组', editRule?.source_chat_type)}
              ${option('private', '私聊', editRule?.source_chat_type)}
              ${option('unknown', '未知', editRule?.source_chat_type)}
            </select></label>
            <label>发送者 ID<input name="source_sender_id" value="${escapeHtml(editRule?.source_sender_id ?? '')}" placeholder="* 或用户 ID" /></label>
            <label>发送者 Bot 标记<select name="source_sender_is_bot">
              ${option('', '任意', boolToSelect(editRule?.source_sender_is_bot))}
              ${option('true', '只匹配 Bot', boolToSelect(editRule?.source_sender_is_bot))}
              ${option('false', '只匹配真人', boolToSelect(editRule?.source_sender_is_bot))}
            </select></label>
          </div>
        </div>
        <div class="step-block">
          <div class="step-index">02</div>
          <div>
            <h2>QQ 目标</h2>
            <p>目标 ID 来自 QQ 官方机器人缓存；如果为空，先在 QQ 目标会话给机器人发一条消息。</p>
          </div>
          <div class="choice-list horizontal" id="qq-targets">${renderTargets(state.qqTargets || [], editRule)}</div>
          <div class="grid-2">
            <label>目标类型<select name="qq_target_type">
              ${['group', 'c2c', 'channel', 'dms'].map((item) => option(item, item, editRule?.qq_target_type || 'group')).join('')}
            </select></label>
            <label>目标 ID<input name="qq_target_id" required value="${escapeHtml(editRule?.qq_target_id || '')}" /></label>
            <label>Guild ID<input name="qq_guild_id" value="${escapeHtml(editRule?.qq_guild_id || '')}" placeholder="频道私信可选" /></label>
            <label>Channel ID<input name="qq_channel_id" value="${escapeHtml(editRule?.qq_channel_id || '')}" placeholder="频道可选" /></label>
          </div>
        </div>
        <div class="step-block">
          <div class="step-index">03</div>
          <div>
            <h2>匹配器</h2>
            <p>关键词模式会自动高亮并参与重复规则合并；高级正则只在需要时使用。</p>
          </div>
          <div class="grid-2">
            <label>匹配模式<select name="match_mode">
              ${option('keywords', '关键词任意命中', editRule?.match_mode || 'keywords')}
              ${option('regex', '高级 include 正则', editRule?.match_mode)}
              ${option('all', '全部消息', editRule?.match_mode)}
            </select></label>
            <label>优先级<input name="priority" type="number" value="${escapeHtml(editRule?.priority ?? 0)}" /></label>
          </div>
          <label>关键词<input name="keywords" value="${escapeHtml((editRule?.keywords || []).join(', '))}" placeholder="AI Python 机器人；支持空格、逗号、分号、换行粘贴" /></label>
          <label>Include 正则<textarea name="text_include_regex" rows="2" placeholder="高级模式使用">${escapeHtml(editRule?.match_mode === 'regex' ? editRule?.text_include_regex || '' : '')}</textarea></label>
          <label>Exclude 正则<textarea name="text_exclude_regex" rows="2" placeholder="命中后排除">${escapeHtml(editRule?.text_exclude_regex || '')}</textarea></label>
          <label>媒体类型<input name="media_types" value="${escapeHtml((editRule?.media_types || []).join(', '))}" placeholder="photo, video；留空不限" /></label>
        </div>
        <div class="step-block">
          <div class="step-index">04</div>
          <div>
            <h2>模板与开关</h2>
            <p>模板变量会由现有 MessageFormatter 渲染，隐藏链接与按钮链接仍会自动补全。</p>
          </div>
          <label class="switch-line"><input name="enabled" type="checkbox" ${editRule?.enabled === false ? '' : 'checked'} /> 保存后立即启用</label>
          <label>消息模板<textarea name="message_template" rows="6" required>${escapeHtml(editRule?.message_template || DEFAULT_TEMPLATE)}</textarea></label>
          <div class="variable-cloud">
            ${(state.options?.template_variables || []).map((item) => `<button type="button" data-action="insert-var" data-var="${item}">{${item}}</button>`).join('')}
          </div>
        </div>
      </section>
      <aside class="panel studio-preview">
        <div class="panel-head"><h2>实时预览</h2>${statusPill('Formatter', 'good')}</div>
        <label>测试文本<textarea name="preview_text" rows="5">AI news from Telegram\nhttps://example.com</textarea></label>
        <button type="button" class="primary" data-action="preview-rule">生成预览</button>
        <pre id="preview-output" class="preview-output">等待预览…</pre>
        <button type="submit" class="save-button">${editRule ? '保存编辑' : '创建规则'}</button>
      </aside>
    </form>
  `;
}

export function bindRuleStudio(root, navigate) {
  const form = root.querySelector('#rule-form');
  if (!form) return;
  root.querySelector('[data-action="search-dialogs"]')?.addEventListener('click', async () => {
    const query = root.querySelector('#dialog-query')?.value || '';
    const container = root.querySelector('#dialog-results');
    container.hidden = false;
    container.innerHTML = '<span class="muted">搜索中…</span>';
    try {
      const items = await api.dialogs(query, 80);
      container.innerHTML = items.length ? items.map(renderDialogChoice).join('') : '<span class="muted">没有匹配会话</span>';
    } catch (error) {
      container.innerHTML = `<span class="muted">${escapeHtml(error.message)}</span>`;
    }
  });
  form.addEventListener('click', async (event) => {
    const target = event.target.closest('button');
    if (!target) return;
    if (target.dataset.dialogId) selectDialog(target, form, root);
    if (target.dataset.targetId) {
      form.qq_target_type.value = target.dataset.targetType;
      form.qq_target_id.value = target.dataset.targetId;
      form.qq_guild_id.value = target.dataset.guildId || '';
      form.qq_channel_id.value = target.dataset.channelId || '';
    }
    if (target.dataset.action === 'insert-var') {
      const textarea = form.message_template;
      textarea.setRangeText(`{${target.dataset.var}}`, textarea.selectionStart, textarea.selectionEnd, 'end');
      textarea.focus();
    }
    if (target.dataset.action === 'preview-rule') {
      await preview(form, root);
    }
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = payloadFromForm(form);
    const editId = form.dataset.editId;
    const tg = window.Telegram?.WebApp;
    try {
      if (editId) {
        await api.updateRule(editId, payload);
      } else {
        await api.createRule(payload);
      }
      tg?.HapticFeedback?.notificationOccurred?.('success');
      await refreshRules();
      navigate('rules');
    } catch (error) {
      tg?.HapticFeedback?.notificationOccurred?.('error');
      root.querySelector('#preview-output').textContent = `保存失败：${error.message}`;
    }
  });
}

function option(value, label, selected) {
  return `<option value="${escapeHtml(value)}" ${String(selected ?? '') === String(value) ? 'selected' : ''}>${escapeHtml(label)}</option>`;
}

function boolToSelect(value) {
  if (value === true) return 'true';
  if (value === false) return 'false';
  return '';
}

function renderTargets(targets, editRule) {
  if (!targets.length) return '<span class="muted">还没有缓存到 QQ 目标。</span>';
  return targets.slice(0, 24).map((target) => `
    <button type="button" class="choice-card ${target.target_id === editRule?.qq_target_id ? 'selected' : ''}" data-target-type="${escapeHtml(target.target_type)}" data-target-id="${escapeHtml(target.target_id)}">
      <strong>${escapeHtml(target.target_type)}</strong>
      <code>${escapeHtml(short(target.target_id, 24))}</code>
      <span>${escapeHtml(target.display_name || target.last_message_id || '-')}</span>
    </button>
  `).join('');
}

function renderDialogChoice(item) {
  return `
    <button type="button" class="choice-card" data-dialog-id="${escapeHtml(item.id)}" data-dialog-type="${escapeHtml(item.type)}" data-dialog-name="${escapeHtml(item.name)}">
      <strong>${escapeHtml(item.type)}</strong>
      <code>${escapeHtml(item.id)}</code>
      <span>${escapeHtml(item.name)}</span>
    </button>
  `;
}

function selectDialog(button, form, root) {
  form.elements.name.value = button.dataset.dialogName || '';
  form.elements.source_chat_id.value = button.dataset.dialogId;
  form.elements.source_chat_type.value = button.dataset.dialogType;
  const container = root.querySelector('#dialog-results');
  if (container) {
    container.innerHTML = '';
    container.hidden = true;
  }
}

function payloadFromForm(form) {
  const value = (name) => form.elements[name]?.value?.trim?.() ?? '';
  const nullableInt = (name) => {
    const text = value(name);
    return text && text !== '*' ? Number(text) : null;
  };
  const nullableBool = (name) => {
    const text = value(name);
    if (text === 'true') return true;
    if (text === 'false') return false;
    return null;
  };
  return {
    name: value('name'),
    enabled: form.elements.enabled.checked,
    source_chat_id: nullableInt('source_chat_id'),
    source_chat_type: value('source_chat_type') || null,
    source_sender_id: nullableInt('source_sender_id'),
    source_sender_is_bot: nullableBool('source_sender_is_bot'),
    match_mode: value('match_mode'),
    keywords: value('keywords'),
    text_include_regex: value('text_include_regex') || null,
    text_exclude_regex: value('text_exclude_regex') || null,
    media_types: value('media_types') || null,
    qq_target_type: value('qq_target_type'),
    qq_target_id: value('qq_target_id'),
    qq_guild_id: value('qq_guild_id') || null,
    qq_channel_id: value('qq_channel_id') || null,
    message_template: value('message_template'),
    priority: Number(value('priority') || 0),
  };
}

async function preview(form, root) {
  const output = root.querySelector('#preview-output');
  output.textContent = '渲染中…';
  try {
    const result = await api.previewRule({
      rule: payloadFromForm(form),
      message: {
        text: form.elements.preview_text.value,
        chat_id: payloadFromForm(form).source_chat_id || -1001234567890,
        chat_title: 'Preview Channel',
        chat_type: payloadFromForm(form).source_chat_type || 'channel',
        sender_id: payloadFromForm(form).source_sender_id || 42,
        sender_username: 'preview_sender',
        sender_display_name: 'Preview Sender',
        sender_is_bot: false,
        media_type: null,
        links: [{ text: 'Open', url: 'https://example.com/open', source: 'button_url' }],
      },
    });
    output.textContent = `匹配：${result.matches ? '是' : '否'}\n关键词：${result.detected_keywords.join('、') || '-'}\n${result.warnings.join('\n')}\n\n${result.rendered_text}`;
  } catch (error) {
    output.textContent = `预览失败：${error.message}`;
  }
}
