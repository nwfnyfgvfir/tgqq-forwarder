import { api } from '../api.js?v=20261111-backup';
import { escapeHtml, pageHeader, statusPill } from '../components.js?v=20261029-rsskw';
import { refreshStatus, state } from '../state.js?v=20261029-rsskw';

let loginPollTimer = null;
let backupStatus = null;
let backupPreview = null;
let backupBusy = false;
let backupMessage = '';
let backupError = false;
let backupStatusLoading = false;

export function renderSystem(state) {
  const status = state.status || {};
  const user = state.me?.user;
  return `
    ${pageHeader('SYSTEM NODE', '系统与部署', '管理运行状态、完整备份与还原。')}
    ${renderBackupPanel()}
    <section class="two-column">
      <article class="panel">
        <div class="panel-head"><h2>Mini App</h2>${statusPill(status.mini_app_enabled ? '已启用' : '未启用', status.mini_app_enabled ? 'good' : 'warning')}</div>
        <dl class="kv">
          <dt>公开 URL</dt><dd>${escapeHtml(status.mini_app_public_url || '未配置')}</dd>
          <dt>当前管理员</dt><dd>${escapeHtml(user ? `${user.display_name} (${user.id})` : '-')}</dd>
          <dt>鉴权方式</dt><dd>Telegram WebApp initData HMAC + 管理员白名单</dd>
        </dl>
      </article>
      <article class="panel">
        <div class="panel-head"><h2>部署检查</h2></div>
        <ol class="deploy-list">
          <li>公网域名必须启用 HTTPS，Telegram 客户端不会接受普通 HTTP。</li>
          <li>反向代理转发到容器内 <code>MINI_APP_PORT</code>。</li>
          <li>BotFather 配置 Menu Button 或通过管理 Bot 的按钮打开。</li>
          <li>非 <code>ADMIN_TELEGRAM_USER_IDS</code> 用户即使打开页面也无法调用 API。</li>
        </ol>
      </article>
    </section>
    ${renderWeixinPanel(status.weixin)}
    <section class="panel">
      <div class="panel-head"><h2>API 读数</h2></div>
      <pre class="json-dump">${escapeHtml(JSON.stringify(status, null, 2))}</pre>
    </section>
  `;
}

function renderWeixinPanel(weixin) {
  if (!weixin) {
    return `
      <section class="panel">
        <div class="panel-head"><h2>个人微信</h2>${statusPill('未配置', 'muted')}</div>
        <p class="muted">设置 <code>WEIXIN_ENABLED=true</code> 并重启后，可以扫码登录个人微信，把消息转发到 QQ，或把 Telegram 消息转发到微信。</p>
      </section>
    `;
  }
  const login = weixin.login;
  const pending = Boolean(login && login.pending);
  const qrBlock = pending
    ? `
      <div class="weixin-login">
        ${login.image_data_url
          ? `<img alt="微信登录二维码" src="${login.image_data_url}" />`
          : `<div class="weixin-login-fallback"><p>二维码图片未能生成</p><code>${escapeHtml(login.scan_url || login.qrcode || '-')}</code></div>`}
        <div>
          <p>${login.status === 'scaned' ? '已扫码，请在手机上确认登录。' : '请用微信「扫一扫」扫描二维码。'}</p>
          ${login.image_url ? `<p><a href="${escapeHtml(login.image_url)}" target="_blank" rel="noreferrer">二维码链接</a></p>` : ''}
          ${login.error ? `<p class="muted">${escapeHtml(login.error)}</p>` : ''}
        </div>
      </div>
    `
    : '';
  return `
    <section class="panel">
      <div class="panel-head"><h2>个人微信</h2>${statusPill(weixin.logged_in ? '已登录' : '未登录', weixin.logged_in ? 'good' : 'warning')}</div>
      <dl class="kv">
        <dt>账号</dt><dd>${escapeHtml(weixin.account_user_id || weixin.account_id)}</dd>
        <dt>API 地址</dt><dd>${escapeHtml(weixin.base_url)}</dd>
        <dt>已缓存联系人</dt><dd>${escapeHtml(weixin.contacts)}</dd>
        <dt>收 / 发</dt><dd>${escapeHtml(weixin.inbound_total)} / ${escapeHtml(weixin.outbound_total)}</dd>
        <dt>失败次数</dt><dd>${escapeHtml(weixin.error_total)}</dd>
        <dt>挂起待补发</dt><dd>${escapeHtml(weixin.pending_outbound ?? 0)}</dd>
        <dt>最近错误</dt><dd>${escapeHtml(weixin.last_error || '-')}</dd>
      </dl>
      ${qrBlock}
      <div class="card-actions">
        <button data-action="weixin-login">${pending ? '刷新二维码' : '扫码登录'}</button>
        <button class="danger" data-action="weixin-logout" ${weixin.logged_in ? '' : 'disabled'}>退出登录</button>
      </div>
      <p class="muted" id="weixin-login-hint">微信协议要求：只能回复最近主动给本号发过消息的联系人（context_token）。</p>
    </section>
  `;
}

export function bindSystem(root) {
  bindBackups(root);
  if (!backupBusy && !backupStatus && !backupStatusLoading) {
    backupStatusLoading = true;
    api.backupStatus().then((status) => {
      backupStatus = status;
      updateBackupPanel(root);
    }).catch((error) => {
      backupMessage = `备份状态读取失败：${error.message}`;
      backupError = true;
      updateBackupPanel(root);
    }).finally(() => { backupStatusLoading = false; });
  }
  const hint = () => root.querySelector('#weixin-login-hint');
  root.querySelector('[data-action="weixin-login"]')?.addEventListener('click', async () => {
    const box = hint();
    if (box) box.textContent = '正在申请二维码…';
    try {
      await api.weixinLogin(true);
      await refreshStatus();
      startLoginPoll();
    } catch (error) {
      if (box) box.textContent = `申请二维码失败：${error.message}`;
    }
  });
  root.querySelector('[data-action="weixin-logout"]')?.addEventListener('click', async () => {
    const box = hint();
    if (box) box.textContent = '正在退出登录…';
    try {
      await api.weixinLogout();
      await refreshStatus();
    } catch (error) {
      if (box) box.textContent = `退出失败：${error.message}`;
    }
  });
}

function sizeLabel(bytes) {
  const amount = Number(bytes || 0);
  if (amount >= 1024 ** 3) return `${(amount / 1024 ** 3).toFixed(2)} GB`;
  if (amount >= 1024 ** 2) return `${(amount / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.ceil(amount / 1024)} KB`;
}

function renderBackupPanel() {
  const preview = backupPreview;
  const last = backupStatus?.last_restore;
  const pending = backupStatus?.pending_restore;
  const localDate = (value) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
  return `
    <section class="panel backup-panel" id="backup-panel" aria-busy="${backupBusy}">
      <div class="panel-head"><div><p class="eyebrow">FULL SNAPSHOT</p><h2>完整备份与还原</h2></div>${statusPill(pending ? '等待重启还原' : '全部应用数据', pending ? 'warning' : 'neutral')}</div>
      <p class="muted">规则与目标、RSS 订阅和历史、转发日志、全部配置、Telegram / 微信登录会话、媒体及日志文件。</p>
      <div class="backup-actions">
        <div class="backup-action">
          <span class="backup-step">01 / 导出</span><h3>保存完整备份</h3>
          <p class="muted">下载一个 ZIP 文件，用于迁移或恢复到当前时点。</p>
          <button data-backup-action="download" ${backupBusy || pending ? 'disabled' : ''}>下载完整备份</button>
        </div>
        <div class="backup-action">
          <span class="backup-step">02 / 还原</span><h3>从备份恢复</h3>
          <p class="muted">先检查文件，再确认覆盖。还原前会自动保留当前数据的备份。</p>
          <label class="backup-file-label">选择备份文件<input id="backup-file" type="file" accept=".zip,application/zip" ${backupBusy || pending ? 'disabled' : ''} /></label>
          ${backupStatus ? `<p class="muted">上传上限 ${escapeHtml(sizeLabel(backupStatus.max_archive_bytes))}</p>` : ''}
        </div>
      </div>
      <p class="backup-sensitive">备份包含 API Key、机器人凭据和登录会话，请仅保存到可信位置、仅还原可信备份。</p>
      ${preview ? `
        <div class="backup-preview">
          <h3>备份已通过校验</h3>
          <dl class="kv"><dt>备份时间</dt><dd>${escapeHtml(localDate(preview.created_at))}</dd><dt>文件数量</dt><dd>${escapeHtml(preview.file_count)}</dd><dt>原始数据</dt><dd>${escapeHtml(sizeLabel(preview.unpacked_bytes))}</dd></dl>
          <p class="muted">${(preview.includes || []).map(escapeHtml).join(' · ')}</p>
          <label class="backup-confirm"><input id="backup-confirm" type="checkbox" ${backupBusy || pending ? 'disabled' : ''} />我确认用此备份覆盖当前全部应用数据，并重启服务。</label>
          <button class="danger" data-backup-action="restore" disabled>确认还原并重启</button>
        </div>` : ''}
      <p id="backup-message" class="${backupError ? 'backup-error' : 'muted'}" role="status" aria-live="polite">${escapeHtml(backupMessage)}</p>
      ${last ? `<p class="${last.status === 'success' ? 'muted' : 'backup-error'}">最近还原：${escapeHtml(last.message)} · ${escapeHtml(localDate(last.finished_at))}</p>` : ''}
      ${(backupStatus?.rollback_backups || []).length ? `<details class="backup-history"><summary>还原前保留的备份（${backupStatus.rollback_backups.length}）</summary>${backupStatus.rollback_backups.map((item) => `<div class="backup-history-row"><span>${escapeHtml(localDate(item.created_at))} · ${escapeHtml(sizeLabel(item.bytes))}</span><button data-rollback-id="${escapeHtml(item.id)}" ${backupBusy ? 'disabled' : ''}>下载</button></div>`).join('')}</details>` : ''}
    </section>
  `;
}

function updateBackupPanel(root) {
  const panel = root.querySelector('#backup-panel');
  if (!panel) return;
  panel.outerHTML = renderBackupPanel();
  bindBackups(root);
}

function saveBackup(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

async function backupOperation(root, message, operation) {
  if (backupBusy) return;
  backupBusy = true;
  backupError = false;
  backupMessage = message;
  updateBackupPanel(root);
  try {
    await operation();
  } catch (error) {
    backupError = true;
    backupMessage = error.message;
  } finally {
    backupBusy = false;
    updateBackupPanel(root);
  }
}

function bindBackups(root) {
  root.querySelector('[data-backup-action="download"]')?.addEventListener('click', () => backupOperation(root, '正在创建完整备份，完成后将开始下载…', async () => {
    const { blob } = await api.downloadBackup();
    saveBackup(blob, `tgqq-backup-${new Date().toISOString().replace(/[:.]/g, '-')}.zip`);
    backupMessage = '备份已生成，已开始下载。';
  }));
  root.querySelector('#backup-file')?.addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    backupPreview = null;
    if (!file) return;
    if (backupStatus && file.size > backupStatus.max_archive_bytes) {
      backupError = true;
      backupMessage = `文件超过上传上限 ${sizeLabel(backupStatus.max_archive_bytes)}`;
      updateBackupPanel(root);
      return;
    }
    backupOperation(root, `正在上传并校验 ${file.name}…`, async () => {
      backupPreview = await api.inspectBackup(file);
      backupMessage = '请核对备份信息，确认后执行还原。';
    });
  });
  root.querySelector('#backup-confirm')?.addEventListener('change', (event) => {
    const button = root.querySelector('[data-backup-action="restore"]');
    if (button) button.disabled = !event.target.checked || backupBusy || Boolean(backupStatus?.pending_restore);
  });
  root.querySelector('[data-backup-action="restore"]')?.addEventListener('click', () => {
    if (!root.querySelector('#backup-confirm')?.checked || !backupPreview) return;
    const id = backupPreview.id;
    backupOperation(root, '正在安排还原…', async () => {
      const result = await api.restoreBackup(id);
      backupPreview = null;
      backupStatus = { ...(backupStatus || {}), pending_restore: true };
      backupMessage = result.automatic_restart
        ? '服务正在重启并还原，请稍后重新打开管理台。还原结果会显示在这里。'
        : result.message;
    });
  });
  root.querySelectorAll('[data-rollback-id]').forEach((button) => button.addEventListener('click', () => backupOperation(root, '正在下载还原前备份…', async () => {
    const { blob } = await api.downloadRollback(button.dataset.rollbackId);
    saveBackup(blob, 'tgqq-before-restore.zip');
    backupMessage = '还原前备份已开始下载；可选择此文件回退。';
  })));
}

function startLoginPoll() {
  if (loginPollTimer) clearInterval(loginPollTimer);
  const deadline = Date.now() + 180_000;
  loginPollTimer = setInterval(async () => {
    if (Date.now() > deadline) {
      clearInterval(loginPollTimer);
      loginPollTimer = null;
      return;
    }
    try {
      await refreshStatus();
    } catch {
      // 轮询失败时静默重试，页面本身会展示后端错误。
    }
    const weixin = state.status?.weixin;
    if (weixin?.logged_in) {
      clearInterval(loginPollTimer);
      loginPollTimer = null;
    }
  }, 3000);
}
