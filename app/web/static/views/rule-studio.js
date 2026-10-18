import { api } from '../api.js?v=20261018-tgtg2';
import { escapeHtml, pageHeader, short, statusPill } from '../components.js?v=20261018-tgtg2';
import { refreshRules } from '../state.js?v=20261018-tgtg2';

const DEFAULT_TEMPLATE = '# {source_title_md}\n\n**发送者：{sender_name_md}**\n\n{text}\n\n{footer_note}';
const TARGET_TYPES_BY_PLATFORM = {
  qq: ['group', 'c2c', 'channel', 'dms'],
  weixin: ['friend'],
  telegram: ['chat', 'bot'],
};
const TARGET_TYPE_LABELS = {
  group: '群',
  c2c: '私聊',
  channel: '频道',
  dms: '频道私信',
  friend: '好友',
  chat: '用户账号发送',
  bot: 'Bot 发送',
};
// 渲染时由 state.options 填充，供目标行的账号下拉使用。
let telegramAccountIds = [];

export function renderRuleStudio(state) {
  const editRule = state.routeParams?.rule || null;
  const sourcePlatform = editRule?.source_platform || '';
  const weixinAccountId = state.options?.weixin_account_id;
  telegramAccountIds = state.options?.telegram_account_ids || [];
  const initialTargets = targetsOfRule(editRule);
  return `
    ${pageHeader(
      'RULE STUDIO',
      editRule?.id ? `编辑规则 #${editRule.id}` : '规则工坊',
      '来源可以是 Telegram、微信或 QQ；一条规则可同时投递到多个 QQ / 微信 / Telegram 目标。用选择器、关键词芯片、模板预览完成配置。'
    )}
    <form id="rule-form" class="studio-layout" data-edit-id="${editRule?.id || ''}">
      <section class="panel studio-main">
        <div class="step-block">
          <div class="step-index">01</div>
          <div>
            <h2>消息来源</h2>
            <p>先选平台，再选监听账号与来源会话；平台或账号留空代表任意。微信来源只能收到好友私聊，QQ 来源是机器人收到的群 / 频道 / 私聊消息（只有一个机器人实例，没有账号维度）。</p>
          </div>
          <label>规则名称<input name="name" required maxlength="120" value="${escapeHtml(editRule?.name || '')}" placeholder="例如：LINUX DO AI 关键词" /></label>
          <div class="grid-2">
            <label>来源平台<select name="source_platform">
              ${option('', '任意平台', sourcePlatform)}
              ${option('telegram', 'Telegram', sourcePlatform)}
              ${option('weixin', '个人微信', sourcePlatform)}
              ${option('qq', 'QQ 机器人', sourcePlatform)}
            </select></label>
            <label>监听账号<select name="source_account_id">
              ${option('', '任意账号', editRule?.source_account_id || '')}
              ${(state.options?.telegram_account_ids || []).map((item) => option(item, `TG · ${item}`, editRule?.source_account_id || '')).join('')}
              ${weixinAccountId ? option(weixinAccountId, `微信 · ${weixinAccountId}`, editRule?.source_account_id || '') : ''}
            </select></label>
          </div>
          <p class="muted" data-qq-account-hint hidden>QQ 来源没有账号维度：程序只连一个 QQ 机器人，选中它时监听账号固定为「任意账号」。</p>
          <div class="grid-2">
            <label>来源会话 ID<input name="source_chat_id" value="${escapeHtml(editRule?.source_chat_id ?? '')}" placeholder="* / -100... / wxid_... / QQ openid" /></label>
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
            <label>发送者 ID<input name="source_sender_id" value="${escapeHtml(editRule?.source_sender_id ?? '')}" placeholder="* / 用户 ID / wxid / QQ openid" /></label>
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
            <p>一条规则可以同时发到多个目标：QQ 群/频道、微信好友、Telegram 会话都可以混排。点下面的卡片可直接追加，也可以手工填写。</p>
          </div>
          <div id="target-list" class="target-list">${initialTargets.map((item) => targetRowHtml(item)).join('')}</div>
          <div class="target-toolbar">
            <button type="button" data-action="add-target">＋ 添加目标</button>
            <span class="muted">每个目标单独发送、单独记日志，一个失败不影响其它目标。</span>
          </div>
          <div class="target-sources">
            <div>
              <h3>QQ 缓存目标</h3>
              <div class="choice-list horizontal" id="qq-targets">${renderTargets(state.qqTargets || [], initialTargets)}</div>
            </div>
            <div>
              <h3>微信联系人</h3>
              <div class="choice-list horizontal" id="weixin-contacts">${renderWeixinContacts(state.options?.weixin_contacts || [], initialTargets)}</div>
              <p class="muted">没有联系人？让对方先给本微信号发一条消息，再刷新本页即可。</p>
            </div>
            <div>
              <h3>Telegram 会话</h3>
              <div class="picker-row">
                <input id="tg-target-query" placeholder="搜索会话名或 ID" />
                <button type="button" data-action="search-tg-targets">搜索</button>
              </div>
              <div class="choice-list horizontal" id="tg-target-results" hidden></div>
              <p class="muted">目标 ID 也可以手填数字 chat_id、@频道用户名，或 <code>me</code>（收藏夹，仅用户账号发送）。</p>
            </div>
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
  let tgSearchSeq = 0;
  syncPlatformPanels(form);
  form.querySelectorAll('[data-target-row]').forEach(syncTargetRow);
  form.addEventListener('change', (event) => {
    if (event.target?.name === 'source_platform') {
      syncPlatformPanels(form);
    }
    if (event.target?.dataset?.field === 'platform') {
      syncTargetRow(event.target.closest('[data-target-row]'));
    }
    if (event.target?.dataset?.field === 'target_type') {
      syncTargetRow(event.target.closest('[data-target-row]'));
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
  root.querySelector('[data-action="search-tg-targets"]')?.addEventListener('click', async () => {
    const query = root.querySelector('#tg-target-query')?.value || '';
    const container = root.querySelector('#tg-target-results');
    if (!container) return;
    const searchSeq = ++tgSearchSeq;
    container.hidden = false;
    container.innerHTML = '<span class="muted">搜索中…</span>';
    try {
      const items = await api.dialogs(query, 40, '');
      if (searchSeq !== tgSearchSeq) return;
      container.hidden = false;
      container.innerHTML = items.length
        ? items.map(renderTelegramChoice).join('')
        : '<span class="muted">没有匹配会话</span>';
    } catch (error) {
      if (searchSeq !== tgSearchSeq) return;
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
    if (target.dataset.tgTargetId) {
      tgSearchSeq += 1;
      addTargetRow(form, {
        platform: 'telegram',
        target_type: 'chat',
        target_id: target.dataset.tgTargetId || '',
        account_id: target.dataset.tgAccount || '',
      });
      const container = root.querySelector('#tg-target-results');
      if (container) {
        container.replaceChildren();
        container.hidden = true;
      }
      return;
    }
    if (target.dataset.weixinUser) {
      addTargetRow(form, {
        platform: 'weixin',
        target_type: 'friend',
        target_id: target.dataset.weixinUser || '',
      });
    }
    if (target.dataset.targetId) {
      addTargetRow(form, {
        platform: 'qq',
        target_type: target.dataset.targetType || 'group',
        target_id: target.dataset.targetId || '',
        guild_id: target.dataset.guildId || '',
        channel_id: target.dataset.channelId || '',
      });
    }
    if (target.dataset.action === 'add-target') {
      addTargetRow(form, {});
    }
    if (target.dataset.action === 'remove-target') {
      target.closest('[data-target-row]')?.remove();
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
    const output = root.querySelector('#preview-output');
    if (!payload.targets.length) {
      if (output) output.textContent = '保存失败：至少需要一个投递目标（填好目标 ID 或点上方缓存卡片）。';
      return;
    }
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
      if (output) output.textContent = `保存失败：${error.message}`;
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

function platformOf(target) {
  if (target?.platform === 'weixin') return 'weixin';
  if (target?.platform === 'telegram') return 'telegram';
  return 'qq';
}

function targetsOfRule(editRule) {
  if (Array.isArray(editRule?.targets) && editRule.targets.length) return editRule.targets;
  return [{
    platform: editRule?.target_platform || 'qq',
    target_type: editRule?.qq_target_type || 'group',
    target_id: editRule?.qq_target_id || '',
    guild_id: editRule?.qq_guild_id || '',
    channel_id: editRule?.qq_channel_id || '',
    account_id: null,
    enabled: true,
  }];
}

function targetRowHtml(target) {
  const platform = platformOf(target);
  const types = TARGET_TYPES_BY_PLATFORM[platform] || ['group'];
  const type = types.includes(target.target_type) ? target.target_type : types[0];
  const showExtras = platform === 'qq' && (type === 'channel' || type === 'dms');
  const showAccount = platform === 'telegram' && type === 'chat';
  return `
    <div class="target-row" data-target-row>
      <select data-field="platform">
        ${option('qq', 'QQ 机器人', platform)}
        ${option('weixin', '个人微信', platform)}
        ${option('telegram', 'Telegram', platform)}
      </select>
      <select data-field="target_type">
        ${types.map((item) => option(item, TARGET_TYPE_LABELS[item] || item, type)).join('')}
      </select>
      <input data-field="target_id" value="${escapeHtml(target.target_id || '')}" placeholder="QQ openid / 频道 ID / wxid / TG 会话 ID" />
      <select data-field="account_id" ${showAccount ? '' : 'hidden'}>
        ${option('', '默认账号', target.account_id || '')}
        ${telegramAccountIds.map((item) => option(item, `TG · ${item}`, target.account_id || '')).join('')}
      </select>
      <input data-field="guild_id" value="${escapeHtml(target.guild_id || '')}" placeholder="Guild ID（可选）" ${showExtras ? '' : 'hidden'} />
      <input data-field="channel_id" value="${escapeHtml(target.channel_id || '')}" placeholder="Channel ID（可选）" ${showExtras ? '' : 'hidden'} />
      <label class="switch-line"><input type="checkbox" data-field="enabled" ${target.enabled === false ? '' : 'checked'} /> 启用</label>
      <button type="button" class="danger" data-action="remove-target">移除</button>
    </div>
  `;
}

function syncTargetRow(row) {
  if (!row) return;
  const platform = row.querySelector('[data-field="platform"]')?.value || 'qq';
  const typeSelect = row.querySelector('[data-field="target_type"]');
  const accountSelect = row.querySelector('[data-field="account_id"]');
  const guildInput = row.querySelector('[data-field="guild_id"]');
  const channelInput = row.querySelector('[data-field="channel_id"]');
  const types = TARGET_TYPES_BY_PLATFORM[platform] || ['group'];
  if (typeSelect) {
    const current = typeSelect.value;
    typeSelect.innerHTML = types
      .map((item) => option(item, TARGET_TYPE_LABELS[item] || item, types.includes(current) ? current : types[0]))
      .join('');
    typeSelect.value = types.includes(current) ? current : types[0];
    typeSelect.disabled = types.length === 1;
  }
  const type = typeSelect?.value || types[0];
  const showExtras = platform === 'qq' && (type === 'channel' || type === 'dms');
  // Guild / Channel 只对 QQ 频道类目标有意义，其余类型隐藏以免挤占输入宽度。
  for (const input of [guildInput, channelInput]) {
    if (!input) continue;
    input.hidden = !showExtras;
    if (!showExtras) input.value = '';
  }
  const showAccount = platform === 'telegram' && type === 'chat';
  if (accountSelect) {
    accountSelect.hidden = !showAccount;
    if (!showAccount) accountSelect.value = '';
  }
}

function addTargetRow(form, target) {
  const list = form.querySelector('#target-list');
  if (!list) return;
  const platform = platformOf(target);
  const types = TARGET_TYPES_BY_PLATFORM[platform] || ['group'];
  const candidate = {
    platform,
    target_type: types.includes(target.target_type) ? target.target_type : types[0],
    target_id: target.target_id || '',
    guild_id: target.guild_id || '',
    channel_id: target.channel_id || '',
    account_id: target.account_id || '',
    enabled: true,
  };
  const rows = Array.from(list.querySelectorAll('[data-target-row]'));
  const isEmptyRow = (row) => !(row.querySelector('[data-field="target_id"]')?.value.trim() || '');
  if (!candidate.target_id) {
    // 「添加目标」时先复用唯一一个空行，避免堆出一串空行。
    const blank = rows.length === 1 && isEmptyRow(rows[0]) ? rows[0] : null;
    if (blank) {
      blank.querySelector('[data-field="target_id"]')?.focus();
      return;
    }
  }
  if (candidate.target_id) {
    const exists = rows.some((row) => {
      const rowPlatform = row.querySelector('[data-field="platform"]')?.value || 'qq';
      const id = row.querySelector('[data-field="target_id"]')?.value.trim() || '';
      const account = row.querySelector('[data-field="account_id"]')?.value.trim() || '';
      return rowPlatform === candidate.platform
        && id === candidate.target_id
        && account === (candidate.account_id || '');
    });
    if (exists) return;
  }
  list.insertAdjacentHTML('beforeend', targetRowHtml(candidate));
  const row = list.lastElementChild;
  syncTargetRow(row);
  if (!candidate.target_id) row?.querySelector('[data-field="target_id"]')?.focus();
}

function renderTargets(targets, currentTargets) {
  if (!targets.length) return '<span class="muted">还没有缓存到 QQ 目标。</span>';
  const selected = new Set((currentTargets || []).filter((item) => item.platform !== 'weixin').map((item) => item.target_id));
  return targets.slice(0, 24).map((target) => `
    <button type="button" class="choice-card ${selected.has(target.target_id) ? 'selected' : ''}" data-target-type="${escapeHtml(target.target_type)}" data-target-id="${escapeHtml(target.target_id)}">
      <strong>${escapeHtml(target.target_type)}</strong>
      <code>${escapeHtml(short(target.target_id, 24))}</code>
      <span>${escapeHtml(target.display_name || target.last_message_id || '-')}</span>
    </button>
  `).join('');
}

function renderWeixinContacts(contacts, currentTargets) {
  if (!contacts.length) return '<span class="muted">还没有微信联系人。</span>';
  const selected = new Set((currentTargets || []).filter((item) => item.platform === 'weixin').map((item) => item.target_id));
  return contacts.slice(0, 24).map((contact) => `
    <button type="button" class="choice-card ${selected.has(contact.user_id) ? 'selected' : ''}" data-weixin-user="${escapeHtml(contact.user_id)}">
      <strong>${contact.has_context_token ? '可发送' : '待对方发消息'}</strong>
      <code>${escapeHtml(short(contact.user_id, 24))}</code>
      <span>${escapeHtml(contact.name || contact.user_id)}</span>
    </button>
  `).join('');
}

function syncPlatformPanels(form) {
  const source = form.elements.source_platform?.value || '';
  form.querySelectorAll('[data-source-panel]').forEach((panel) => {
    panel.hidden = Boolean(source) && panel.dataset.sourcePanel !== source;
  });
  // QQ 来源只有一个机器人实例，没有账号维度：账号下拉禁用并清空，
  // 避免选到一个永远匹配不上 QQ 消息的账号。
  const isQQ = source === 'qq';
  const accountSelect = form.elements.source_account_id;
  if (accountSelect) {
    accountSelect.disabled = isQQ;
    if (isQQ) accountSelect.value = '';
  }
  const hint = form.querySelector('[data-qq-account-hint]');
  if (hint) hint.hidden = !isQQ;
}

function targetsFromForm(form) {
  return Array.from(form.querySelectorAll('[data-target-row]')).map((row) => {
    const field = (name) => row.querySelector(`[data-field="${name}"]`);
    const platform = field('platform')?.value || 'qq';
    const isWeixin = platform === 'weixin';
    const isTelegram = platform === 'telegram';
    const type = field('target_type')?.value || (isWeixin ? 'friend' : isTelegram ? 'chat' : 'group');
    return {
      platform,
      target_type: isWeixin ? 'friend' : type,
      target_id: field('target_id')?.value.trim() || '',
      guild_id: isWeixin || isTelegram ? null : (field('guild_id')?.value.trim() || null),
      channel_id: isWeixin || isTelegram ? null : (field('channel_id')?.value.trim() || null),
      account_id: isTelegram && type === 'chat' ? (field('account_id')?.value.trim() || null) : null,
      enabled: field('enabled') ? field('enabled').checked : true,
    };
  }).filter((item) => item.target_id);
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

function renderTelegramChoice(item) {
  return `
    <button type="button" class="choice-card" data-tg-target-id="${escapeHtml(item.id)}" data-tg-account="${escapeHtml(item.account_id || '')}">
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
  const targets = targetsFromForm(form);
  const first = targets[0] || { platform: 'qq', target_type: 'group', target_id: '', guild_id: null, channel_id: null };
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
    targets,
    // 兼容字段：服务端会按 targets[0] 重新计算，这里只是让旧接口也能读懂。
    target_platform: first.platform,
    qq_target_type: first.target_type,
    qq_target_id: first.target_id,
    qq_guild_id: first.guild_id,
    qq_channel_id: first.channel_id,
    message_template: value('message_template'),
    priority: Number(value('priority') || 0),
  };
}

async function preview(form, root) {
  const output = root.querySelector('#preview-output');
  output.textContent = '渲染中…';
  try {
    const formPayload = payloadFromForm(form);
    const rulePayload = formPayload.targets.length
      ? formPayload
      : { ...formPayload, targets: [{ platform: 'qq', target_type: 'group', target_id: 'preview-target', enabled: true }] };
    const sourcePlatform = formPayload.source_platform || 'telegram';
    const isWeixin = sourcePlatform === 'weixin';
    const isQQ = sourcePlatform === 'qq';
    const result = await api.previewRule({
      rule: rulePayload,
      message: {
        text: form.elements.preview_text.value,
        source_platform: sourcePlatform,
        account_id: formPayload.source_account_id || null,
        chat_id: formPayload.source_chat_id || (isWeixin ? 'wxid_preview' : isQQ ? 'qq_group_openid' : -1001234567890),
        chat_title: isWeixin ? '微信好友' : isQQ ? 'QQ群 12345678' : 'Preview Channel',
        chat_type: formPayload.source_chat_type || (isWeixin || isQQ ? 'private' : 'channel'),
        sender_id: formPayload.source_sender_id || (isWeixin ? 'wxid_preview' : isQQ ? 'qq_member_openid' : 42),
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
