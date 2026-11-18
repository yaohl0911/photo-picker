// 未实现页面的统一「开发中」占位视图。
// 总览/对比（Task 11）与偏好/历史（Task 12）落地后，各页面模块
// 将替换为自己的实现——本模块届时仅作为空态兜底保留与否由后续 task 决定。

const ICON = `
  <svg class="placeholder__icon" viewBox="0 0 48 48" aria-hidden="true">
    <rect x="7" y="12" width="34" height="26" rx="4" fill="none"
          stroke="currentColor" stroke-width="2.4"/>
    <path d="M7 31.5l9-9 7.5 7.5 6-6 12.5 12.5"
          fill="none" stroke="currentColor" stroke-width="2.4"
          stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="18" cy="20.5" r="3.6" fill="currentColor"/>
  </svg>`;

/**
 * 渲染占位页。
 * @param {HTMLElement} container 页面容器（#app）
 * @param {{title: string, description: string, plannedTask: string}} opts
 */
export function renderPlaceholder(container, { title, description, plannedTask }) {
  const section = document.createElement('section');
  section.className = 'placeholder';
  section.dataset.testid = 'placeholder';

  const iconWrap = document.createElement('div');
  iconWrap.className = 'placeholder__icon-wrap';
  iconWrap.innerHTML = ICON;

  const h1 = document.createElement('h1');
  h1.className = 'placeholder__title';
  h1.textContent = title;

  const p = document.createElement('p');
  p.className = 'placeholder__desc';
  p.textContent = description;

  const badge = document.createElement('span');
  badge.className = 'placeholder__badge';
  badge.textContent = plannedTask;

  section.append(iconWrap, h1, p, badge);
  container.appendChild(section);
}
