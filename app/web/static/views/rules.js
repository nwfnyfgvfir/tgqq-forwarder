import { emptyState, pageHeader, ruleCard } from '../components.js?v=20260707-rule-studio-2';

export function renderRules(state) {
  const rules = state.rules || [];
  const cards = rules.length
    ? rules.map(ruleCard).join('')
    : emptyState('规则库为空', '从规则工坊创建第一条关键词转发规则。', '<button class="primary" data-route="studio">新建规则</button>');
  return `
    ${pageHeader('RULE ARCHIVE', '规则库', '集中编辑、复制、停用或删除所有 TG → QQ 转发规则。', '<button class="primary" data-route="studio">新建规则</button>')}
    <section class="panel filter-panel">
      <input id="rule-search" placeholder="搜索规则名、关键词、TG ID 或 QQ 目标" autocomplete="off" />
      <div class="filter-note">当前显示 ${rules.length} 条规则。关键词规则会自动解码显示。</div>
    </section>
    <section class="rule-grid" id="rule-grid">${cards}</section>
  `;
}

export function filterRules() {
  const input = document.querySelector('#rule-search');
  const grid = document.querySelector('#rule-grid');
  if (!input || !grid) return;
  input.addEventListener('input', () => {
    const query = input.value.trim().toLowerCase();
    grid.querySelectorAll('.rule-card').forEach((card) => {
      card.hidden = query && !card.textContent.toLowerCase().includes(query);
    });
  });
}
