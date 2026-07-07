import { escapeHtml, pageHeader, statusPill } from '../components.js?v=20260707-rule-studio-2';

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
    <section class="panel">
      <div class="panel-head"><h2>API 读数</h2></div>
      <pre class="json-dump">${escapeHtml(JSON.stringify(status, null, 2))}</pre>
    </section>
  `;
}
