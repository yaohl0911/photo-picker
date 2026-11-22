// SPA hash 路由与页面注册表。
//
// 路由表：#/overview（默认）、#/compare、#/preferences、#/history、#/settings。
// 每个页面模块导出 render(container) -> cleanup?：
//   - 在 container（#app）内渲染自身；
//   - 返回的 cleanup 函数（可选）在离开页面/切换路由前被调用，
//     用于清理轮询定时器等资源。
//
// 主题引导：模块体最前（import 之后）读 localStorage 的 photo-picker.theme
// 并设置 document.documentElement.dataset.theme，先于首次 render() 执行，
// 防止深色用户看到浅色闪烁（FOUC）。

import { renderOverview } from './pages/overview.js';
import { renderCompare } from './pages/compare.js';
import { renderPreferences } from './pages/preferences.js';
import { renderHistory } from './pages/history.js';
import { renderSettings } from './pages/settings.js';
import { renderScreenshots } from './pages/screenshots.js';

// --- 主题引导（防 FOUC）-----------------------------------------------
// localStorage: 'photo-picker.theme' ∈ 'light' | 'dark' | 'auto'（默认 auto）。
// light/dark → documentElement.dataset.theme；auto → 不设置属性，
// 由 CSS 的 prefers-color-scheme 媒体查询跟随系统。三态互斥。
const THEME_KEY = 'photo-picker.theme';
try {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === 'light' || stored === 'dark') {
    document.documentElement.dataset.theme = stored;
  } else {
    delete document.documentElement.dataset.theme;
  }
} catch (_) {
  /* localStorage 不可用（隐私模式等）时静默回落到跟随系统 */
}

const ROUTES = {
  overview: { label: '总览', render: renderOverview },
  compare: { label: '对比决策', render: renderCompare },
  screenshots: { label: '截图清理', render: renderScreenshots },
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

/** 提取 hash 中的 query 参数（如 `#/compare?group=5` → `group=5`）。 */
function queryFromHash() {
  const raw = (location.hash || '').replace(/^#\/?/, '');
  const qIdx = raw.indexOf('?');
  const query = qIdx === -1 ? '' : raw.slice(qIdx + 1);
  // 过滤非法 query（不能以 / 开头等）
  if (!query || query.includes('/') || query.includes('#')) return '';
  return query;
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
  const query = queryFromHash();
  const canonical = '#/' + name + (query ? '?' + query : '');

  // 未知 hash 归一化（不产生多余历史记录；hashchange 会再次触发 render）
  if ((location.hash || '') !== canonical) {
    location.replace(canonical);
    return;
  }

  if (typeof activeCleanup === 'function') activeCleanup();
  activeCleanup = null;

  appEl.dataset.page = name;
  appEl.replaceChildren();
  document.title = `照片精选助手 · ${ROUTES[name].label}`;
  setActiveNav(name);

  const cleanup = ROUTES[name].render(appEl);
  activeCleanup = typeof cleanup === 'function' ? cleanup : null;
}

window.addEventListener('hashchange', render);
render();
