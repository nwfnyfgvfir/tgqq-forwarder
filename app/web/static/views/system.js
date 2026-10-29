import { api } from '../api.js?v=20261029-rsskw';
import { escapeHtml, pageHeader, statusPill } from '../components.js?v=20261029-rsskw';
import { refreshStatus, state } from '../state.js?v=20261029-rsskw';

let loginPollTimer = null;

export function renderSystem(state) {
  const status = state.status || {};
  const user = state.me?.user;
  return `
    ${pageHeader('SYSTEM NODE', '系统与部署', '只展示运行与部署提示；不会暴露 token、session、secret 或本地敏感路径。')}
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
