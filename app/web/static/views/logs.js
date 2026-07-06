import { emptyState, logRow, pageHeader } from '../components.js';

export function renderLogs(state) {
  const logs = state.logs || [];
  return `
    ${pageHeader('EVENT STREAM', '转发日志', '查看最近转发、错误与目标链路，快速定位规则或 QQ 发送问题。')}
    <section class="panel filter-panel">
      <button data-action="logs-filter" data-status="">全部</button>
      <button data-action="logs-filter" data-status="success">成功</button>
      <button data-action="logs-filter" data-status="failed">错误</button>
      <button data-action="logs-filter" data-status="skipped">跳过</button>
    </section>
    <section class="log-list">
      ${logs.length ? logs.map(logRow).join('') : emptyState('暂无日志', '命中规则并完成 QQ 发送后会在这里出现。')}
    </section>
  `;
}
