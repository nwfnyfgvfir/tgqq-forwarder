import { api } from '../api.js?v=20261018-weixin-1';
import { escapeHtml, pageHeader, short, statusPill } from '../components.js?v=20261018-weixin-1';
import { refreshRules } from '../state.js?v=20261018-weixin-1';

const DEFAULT_TEMPLATE = '# {source_title_md}\n\n**发送者：{sender_name_md}**\n\n{text}\n\n{footer_note}';

export function renderRuleStudio(state) {
  const editRule = state.routeParams?.rule || null;
  const targetPlatform = editRule?.target_platform || 'qq';
  const sourcePlatform = editRule?.source_platform || '';
  const weixinAccountId = state.options?.weixin_account_id;
  return `
    ${pageHeader(
      'RULE STUDIO',
      editRule?.id ? `编辑规则 #${editRule.id}` : '规则工坊',
      '来源可以是 Telegram 或微信，目标可以是 QQ 或微信；用选择器、关键词芯片、模板预览完成配置。'
    )}
    <form id="rule-form" class="studio-layout" data-edit-id="${editRule?.id || ''}" data-target-platform="${escapeHtml(targetPlatform)}">
      <section class="panel studio-main">
        <div class="step-block">
          <div class="step-index">01</div>
          <div>
            <h2>消息来源</h2>
            <p>先选平台，再选监听账号与来源会话；平台或账号留空代表任意。微信来源只能收到好友私聊。</p>
          </div>
          <label>规则名称<input name="name" required maxlength="120" value="${escapeHtml(editRule?.name || '')}" placeholder="例如：LINUX DO AI 关键词" /></label>
          <div class="grid-2">
            <label>来源平台<select name="source_platform">
              ${option('', '任意平台', sourcePlatform)}
              ${option('telegram', 'Telegram', sourcePlatform)}
              ${option('weixin', '个人微信', sourcePlatform)}
            </select></label>
            <label>监听账号<select name="source_account_id">
              ${option('', '任意账号', editRule?.source_account_id || '')}
              ${(state.options?.telegram_account_ids || []).map((item) => option(item, `TG · ${item}`, editRule?.source_account_id || '')).join('')}
              ${weixinAccountId ? option(weixinAccountId, `微信 · ${weixinAccountId}`, editRule?.source_account_id || '') : ''}
            </select></label>
          </div>
          <div class="grid-2">
            <label>来源会话 ID<input name="source_chat_id" value="${escapeHtml(editRule?.source_chat_id ?? '')}" placeholder="* / -100... / wxid_..." /></label>
            <label>会话类型<select name="source_chat_type">
              ${option('', '任意', editRule?.source_chat_type)}
              ${option('channel', '频道', editRule?.source_chat_type)}
              ${option('group', '群组', editRule?.source_chat_type)}
              ${option('private', '私聊', editRule?.source_chat_type)}
              ${option('unknown', '未知', editRule?.source_chat_type)}
            </select></label>
          </div>
          <div class="picker-row" data-source-panel="telegram">
            <input id="dialog-query" placeholder="搜索 Telegram 会话名或 ID" />
            <button type="button" data-action="search-dialogs">搜索会话</button>
          </div>
          <div class="choice-list" id="dialog-results" data-source-panel="telegram" hidden></div>
          <div class="grid-2">
            <label>发送者 ID<input name="source_sender_id" value="${escapeHtml(editRule?.source_sender_id ?? '')}" placeholder="* / 用户 ID / wxid" /></label>
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
            <h2>投递目标</h2>
            <p>QQ 目标 ID 来自官方机器人缓存；微信只能发给最近主动给本号发过消息的联系人（协议要求 context_token）。</p>
          </div>
          <div class="grid-2">
            <label>目标平台<select name="target_platform">
              ${option('qq', 'QQ 机器人', targetPlatform)}
              ${option('weixin', '个人微信', targetPlatform)}
            </select></label>
            <label>目标 ID<input name="qq_target_id" required value="${escapeHtml(editRule?.qq_target_id || '')}" placeholder="QQ openid 或微信 wxid" /></label>
          </div>
          <div data-target-panel="qq">
            <div class="choice-list horizontal" id="qq-targets">${renderTargets(state.qqTargets || [], editRule)}</div>
            <div class="grid-2">
              <label>目标类型<select name="qq_target_type">
                ${['group', 'c2c', 'channel', 'dms', 'friend'].map((item) => option(item, item, editRule?.qq_target_type || 'group')).join('')}
              </select></label>
              <label>Guild ID<input name="qq_guild_id" value="${escapeHtml(editRule?.qq_guild_id || '')}" placeholder="频道私信可选" /></label>
              <label>Channel ID<input name="qq_channel_id" value="${escapeHtml(editRule?.qq_channel_id || '')}" placeholder="频道可选" /></label>
            </div>
          </div>
          <div data-target-panel="weixin">
            <div class="choice-list horizontal" id="weixin-contacts">${renderWeixinContacts(state.options?.weixin_contacts || [], editRule)}</div>
            <p class="muted">没有联系人？让对方先给本微信号发一条消息，再刷新本页即可。</p>
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
            <p>模板变量会由 MessageFormatter 渲染，隐藏链接与按钮链接会优先嵌入或以 Markdown 链接补全。</p>
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
        <button type="submit" class="save-button">${editRule?.id ? '保存编辑' : '创建规则'}</button>
      </aside>
    </form>
  `;
}

export function bindRuleStudio(root, navigate) {
  const form = root.querySelector('#rule-form');
  if (!form) return;
  let dialogSearchSeq = 0;
  syncPlatformPanels(form);
  form.addEventListener('change', (event) => {
    if (event.target?.name === 'target_platform' || event.target?.name === 'source_platform') {
      syncPlatformPanels(form);
    }
  });
  root.querySelector('[data-action="search-dialogs"]')?.addEventListener('click', async () => {
    const query = root.querySelector('#dialog-query')?.value || '';
    const account = form.elements.source_account_id?.value || '';
    const container = root.querySelector('#dialog-results');
    if (!container) return;
    const searchSeq = ++dialogSearchSeq;
    container.hidden = false;
    container.innerHTML = '<span class="muted">搜索中…</span>';
    try {
      const items = await api.dialogs(query, 80, account);
      if (searchSeq !== dialogSearchSeq) return;
      container.hidden = false;
      container.innerHTML = items.length ? items.map(renderDialogChoice).join('') : '<span class="muted">没有匹配会话</span>';
    } catch (error) {
      if (searchSeq !== dialogSearchSeq) return;
      container.hidden = false;
      container.innerHTML = `<span class="muted">${escapeHtml(error.message)}</span>`;
    }
  });
  form.addEventListener('click', async (event) => {
    const target = buttonFromEvent(event, form);
    if (!target) return;
    if (target.matches('#dialog-results button[data-dialog-id]')) {
      dialogSearchSeq += 1;
      selectDialog(target, form, root);
      return;
    }
    if (target.dataset.weixinUser) {
      setFormValue(form, 'target_platform', 'weixin');
      setFormValue(form, 'qq_target_id', target.dataset.weixinUser || '');
      syncPlatformPanels(form);
    }
    if (target.dataset.targetId) {
      setFormValue(form, 'target_platform', 'qq');
      setFormValue(form, 'qq_target_type', target.dataset.targetType || '');
      setFormValue(form, 'qq_target_id', target.dataset.targetId || '');
      setFormValue(form, 'qq_guild_id', target.dataset.guildId || '');
      setFormValue(form, 'qq_channel_id', target.dataset.channelId || '');
      syncPlatformPanels(form);
    }
    if (target.dataset.action === 'insert-var') {
      const textarea = form.elements.namedItem('message_template');
      if (textarea && 'setRangeText' in textarea) {
        textarea.setRangeText(`{${target.dataset.var}}`, textarea.selectionStart, textarea.selectionEnd, 'end');
        textarea.focus();
      }
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

function buttonFromEvent(event, scope) {
  const pathButton = event.composedPath?.().find((node) => node instanceof HTMLButtonElement && scope.contains(node));
  if (pathButton) return pathButton;
  if (event.target instanceof Element) {
    const button = event.target.closest('button');
    return button && scope.contains(button) ? button : null;
  }
  return null;
}

function setFormValue(form, name, value) {
  const control = form.elements.namedItem(name);
  if (control && 'value' in control) {
    control.value = value;
  }
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

function renderWeixinContacts(contacts, editRule) {
  if (!contacts.length) return '<span class="muted">还没有微信联系人。</span>';
  return contacts.slice(0, 24).map((contact) => `
    <button type="button" class="choice-card ${contact.user_id === editRule?.qq_target_id ? 'selected' : ''}" data-weixin-user="${escapeHtml(contact.user_id)}">
      <strong>${contact.has_context_token ? '可发送' : '待对方发消息'}</strong>
      <code>${escapeHtml(short(contact.user_id, 24))}</code>
      <span>${escapeHtml(contact.name || contact.user_id)}</span>
    </button>
  `).join('');
}

function syncPlatformPanels(form) {
  const target = form.elements.target_platform?.value || 'qq';
  form.dataset.targetPlatform = target;
  form.querySelectorAll('[data-target-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.targetPanel !== target;
  });
  const source = form.elements.source_platform?.value || '';
  form.querySelectorAll('[data-source-panel]').forEach((panel) => {
    panel.hidden = Boolean(source) && panel.dataset.sourcePanel !== source;
  });
  const typeSelect = form.elements.qq_target_type;
  if (typeSelect) {
    if (target === 'weixin') {
      if (typeSelect.dataset.savedValue === undefined) typeSelect.dataset.savedValue = typeSelect.value;
      typeSelect.value = 'friend';
      typeSelect.disabled = true;
    } else {
      typeSelect.disabled = false;
      if (typeSelect.dataset.savedValue) {
        typeSelect.value = typeSelect.dataset.savedValue;
        delete typeSelect.dataset.savedValue;
      }
    }
  }
}

function renderDialogChoice(item) {
  return `
    <button type="button" class="choice-card" data-dialog-id="${escapeHtml(item.id)}" data-dialog-type="${escapeHtml(item.type)}" data-dialog-name="${escapeHtml(item.name)}" data-dialog-account="${escapeHtml(item.account_id || '')}">
      <strong>${escapeHtml(item.type)}</strong>
      <code>${escapeHtml(item.id)}</code>
      <span>${escapeHtml(item.name)}${item.account_id ? ` · ${escapeHtml(item.account_id)}` : ''}</span>
    </button>
  `;
}

function selectDialog(button, form, root) {
  setFormValue(form, 'name', button.dataset.dialogName || '');
  setFormValue(form, 'source_platform', 'telegram');
  setFormValue(form, 'source_chat_id', button.dataset.dialogId || '');
  setFormValue(form, 'source_chat_type', button.dataset.dialogType || '');
  if (button.dataset.dialogAccount) {
    setFormValue(form, 'source_account_id', button.dataset.dialogAccount);
  }
  syncPlatformPanels(form);
  const container = root.querySelector('#dialog-results');
  if (container) {
    container.replaceChildren();
    container.hidden = true;
  }
}

function payloadFromForm(form) {
  const value = (name) => form.elements[name]?.value?.trim?.() ?? '';
  const nullableId = (name) => {
    const text = value(name);
    if (!text || text === '*') return null;
    return /^-?\d+$/.test(text) ? Number(text) : text;
  };
  const nullableBool = (name) => {
    const text = value(name);
    if (text === 'true') return true;
    if (text === 'false') return false;
    return null;
  };
  const targetPlatform = value('target_platform') || 'qq';
  const isWeixinTarget = targetPlatform === 'weixin';
  return {
    name: value('name'),
    enabled: form.elements.enabled.checked,
    source_platform: value('source_platform') || null,
    source_account_id: value('source_account_id') || null,
    source_chat_id: nullableId('source_chat_id'),
    source_chat_type: value('source_chat_type') || null,
    source_sender_id: nullableId('source_sender_id'),
    source_sender_is_bot: nullableBool('source_sender_is_bot'),
    match_mode: value('match_mode'),
    keywords: value('keywords'),
    text_include_regex: value('text_include_regex') || null,
    text_exclude_regex: value('text_exclude_regex') || null,
    media_types: value('media_types') || null,
    target_platform: targetPlatform,
    qq_target_type: isWeixinTarget ? 'friend' : value('qq_target_type'),
    qq_target_id: value('qq_target_id'),
    qq_guild_id: isWeixinTarget ? null : value('qq_guild_id') || null,
    qq_channel_id: isWeixinTarget ? null : value('qq_channel_id') || null,
    message_template: value('message_template'),
    priority: Number(value('priority') || 0),
  };
}

async function preview(form, root) {
  const output = root.querySelector('#preview-output');
  output.textContent = '渲染中…';
  try {
    const formPayload = payloadFromForm(form);
    const isWeixin = formPayload.source_platform === 'weixin';
    const result = await api.previewRule({
      rule: formPayload,
      message: {
        text: form.elements.preview_text.value,
        source_platform: formPayload.source_platform || 'telegram',
        account_id: formPayload.source_account_id || null,
        chat_id: formPayload.source_chat_id || (isWeixin ? 'wxid_preview' : -1001234567890),
        chat_title: isWeixin ? '微信好友' : 'Preview Channel',
        chat_type: formPayload.source_chat_type || (isWeixin ? 'private' : 'channel'),
        sender_id: formPayload.source_sender_id || (isWeixin ? 'wxid_preview' : 42),
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
