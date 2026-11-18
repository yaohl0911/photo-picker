// SPA hash 路由与页面注册表。
//
// 路由表：#/overview（默认）、#/compare、#/preferences、#/history、#/settings。
// 每个页面模块导出 render(container) -> cleanup?：
//   - 在 container（#app）内渲染自身；
//   - 返回的 cleanup 函数（可选）在离开页面/切换路由前被调用，
//     用于清理轮询定时器等资源。

import { renderOverview } from './pages/overview.js';
import { renderCompare } from './pages/compare.js';
import { renderPreferences } from './pages/preferences.js';
import { renderHistory } from './pages/history.js';
import { renderSettings } from './pages/settings.js';

const ROUTES = {
  overview: { label: '总览', render: renderOverview },
  compare: { label: '对比决策', render: renderCompare },
  preferences: { label: '偏好', render: renderPreferences },
  history: { label: '历史', render: renderHistory },
  settings: { label: '设置', render: renderSettings },
};

const DEFAULT_ROUTE = 'overview';

const appEl = document.getElementById('app');
const navEl = document.getElementById('main-nav');

let activeCleanup = null;

/** 从 location.hash 解析路由名；未知/为空回落到默认页。 */
function routeFromHash() {
  const raw = (location.hash || '').replace(/^#\/?/, '');
  const name = raw.split(/[?/]/)[0].toLowerCase();
  return Object.prototype.hasOwnProperty.call(ROUTES, name) ? name : DEFAULT_ROUTE;
}

/** 导航高亮：is-active class + aria-current="page"。 */
function setActiveNav(name) {
  navEl.querySelectorAll('a[data-route]').forEach((link) => {
    const active = link.dataset.route === name;
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

function render() {
  const name = routeFromHash();
  const canonical = '#/' + name;

  // 未知 hash 归一化（不产生多余历史记录；hashchange 会再次触发 render）
  if ((location.hash || '') !== canonical) {
    location.replace(canonical);
    return;
  }

  if (typeof activeCleanup === 'function') activeCleanup();
  activeCleanup = null;

  appEl.dataset.page = name;
  appEl.replaceChildren();
  document.title = `照片去重助手 · ${ROUTES[name].label}`;
  setActiveNav(name);

  const cleanup = ROUTES[name].render(appEl);
  activeCleanup = typeof cleanup === 'function' ? cleanup : null;
}

window.addEventListener('hashchange', render);
render();
