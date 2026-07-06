import { emptyState, escapeHtml, formatDate, logRow, pageHeader, statTile, statusPill } from '../components.js';

export function renderDashboard(state) {
  const status = state.status;
  if (!status) {
    return `${pageHeader('CONTROL DECK', '转发控制舱', '正在建立与 Mini App 后端的安全通道。')}<div class="loading-card">载入中…</div>`;
  }
  const telegramState = status.telegram_connected ? 'good' : 'bad';
  const pausedState = status.forwarding_paused ? 'warning' : 'good';
  const recentErrors = (state.logs || []).filter((item) => item.status === 'failed').length;
  const latestLogs = (state.logs || []).slice(0, 5);
  return `
    ${pageHeader(
      'CONTROL DECK',
      '转发控制舱',
      '在 Telegram 内直接管理 TG → QQ 的规则、目标、日志与运行状态。',
      `<button class="primary" data-route="studio">新建规则</button>`
    )}
    <section class="signal-grid">
      ${statTile('Telegram', status.telegram_connected ? '在线' : '断开', '用户账号监听', telegramState)}
      ${statTile('QQ WebSocket', status.qq_status, '官方机器人通道', status.qq_status === 'running' ? 'good' : 'warning')}
      ${statTile('转发开关', status.forwarding_paused ? '已暂停' : '运行中', '全局队列控制', pausedState)}
      ${statTile('队列深度', status.queue_size, '待处理消息', status.queue_size > 0 ? 'warning' : 'neutral')}
    </section>
    <section class="two-column">
      <article class="panel command-panel">
        <div class="panel-head">
          <div>
            <p class="kicker">RULE MATRIX</p>
            <h2>规则矩阵</h2>
          </div>
          ${statusPill(`${status.enabled_rules}/${status.total_rules} 启用`, 'good')}
        </div>
        <div class="big-number">${escapeHtml(status.total_rules)}</div>
        <p>累计日志 ${escapeHtml(status.total_logs)} 条，最近错误 ${escapeHtml(recentErrors)} 条。</p>
        <div class="quick-actions">
          <button class="primary" data-route="studio">打开规则工坊</button>
          <button data-route="rules">查看规则库</button>
          <button class="${status.forwarding_paused ? 'primary' : 'warning'}" data-action="pause" data-paused="${status.forwarding_paused ? 'false' : 'true'}">${status.forwarding_paused ? '恢复转发' : '暂停转发'}</button>
        </div>
      </article>
      <article class="panel">
        <div class="panel-head">
          <div>
            <p class="kicker">RECENT SIGNALS</p>
            <h2>最近转发</h2>
          </div>
          <button data-route="logs">全部日志</button>
        </div>
        <div class="log-list compact">
          ${latestLogs.length ? latestLogs.map(logRow).join('') : emptyState('暂无日志', '收到匹配消息后，这里会显示最新转发结果。')}
        </div>
      </article>
    </section>
    <section class="panel timeline-panel">
      <div class="panel-head">
        <div>
          <p class="kicker">BOOT STATUS</p>
          <h2>运行读数</h2>
        </div>
        <span>${escapeHtml(formatDate(new Date().toISOString()))}</span>
      </div>
      <div class="status-lanes">
        <div>${statusPill(status.telegram_connected ? 'Telegram 已连接' : 'Telegram 未连接', telegramState)}<span>会话选择器依赖该连接。</span></div>
        <div>${statusPill(status.qq_status, status.qq_status === 'running' ? 'good' : 'warning')}<span>QQ 目标来自 WebSocket 缓存。</span></div>
        <div>${statusPill(status.forwarding_paused ? '全局暂停' : '自动转发中', pausedState)}<span>暂停后队列消费会跳过转发。</span></div>
      </div>
    </section>
  `;
}
