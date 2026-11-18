/**
 * util.js — 通用工具（Task 11）：toast 通知与格式化函数。
 * 无依赖，页面模块按需 import。
 */

/* ---------- 格式化 ---------- */

/** 千分位整数。null/undefined → '—'。 */
export function fmtNumber(n) {
  if (n === null || n === undefined) return '—';
  if (typeof n !== 'number' || Number.isNaN(n)) return String(n);
  return n.toLocaleString('zh-Hans-CN');
}

/** ISO/时间戳 → 本地可读时间；无值 → '—'。 */
export function fmtDateTime(v) {
  if (!v) return '—';
  const d = v instanceof Date ? v : new Date(v);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 字节数 → 人类可读（1.2 MB）；无值 → '—'。 */
export function fmtBytes(n) {
  if (n === null || n === undefined) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = Number(n);
  if (!Number.isFinite(v)) return '—';
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** 快门速度：秒。>=1 → 'Ns'；<1 → '1/Ns'（0.002 → '1/500s'）。 */
export function fmtExposure(sec) {
  if (sec === null || sec === undefined || sec === '') return null;
  const v = Number(sec);
  if (!Number.isFinite(v) || v <= 0) return null;
  if (v >= 1) return `${v >= 10 ? Math.round(v) : v.toFixed(1)}s`;
  const den = Math.round(1 / v);
  return `1/${den}s`;
}

/** 光圈：2.8 → 'f/2.8'。无效 → null。 */
export function fmtAperture(f) {
  if (f === null || f === undefined || f === '') return null;
  const v = Number(f);
  if (!Number.isFinite(v) || v <= 0) return null;
  return `f/${v % 1 === 0 ? v : v.toFixed(1)}`;
}

/** 路径 → 文件名。'/a/b/IMG_1.JPG' → 'IMG_1.JPG'。 */
export function basename(p) {
  if (!p) return '';
  const s = String(p);
  const i = s.lastIndexOf('/');
  return i === -1 ? s : s.slice(i + 1);
}

/** 文件大小格式化占位（bytes 字段可能不存在于 files）。 */
export const noValue = '—';

/* ---------- Toast ---------- */

/**
 * 轻量 toast 通知。容器挂在 document.body（跨路由存活，路由 cleanup 不影响）。
 * @param {string} message 文本（支持 \n 换行）
 * @param {object} [opts]
 * @param {'info'|'ok'|'error'|'warn'} [opts.kind]
 * @param {number} [opts.duration] 毫秒；0 = 不自动关闭
 * @returns {() => void} 手动关闭函数
 */
export function toast(message, opts = {}) {
  const kind = opts.kind || 'info';
  const duration = opts.duration === undefined ? 3200 : opts.duration;

  let wrap = document.querySelector('.toast-wrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'toast-wrap';
    document.body.appendChild(wrap);
  }

  const el = document.createElement('div');
  el.className = `toast toast--${kind}`;
  el.setAttribute('role', 'status');
  el.textContent = message;
  wrap.appendChild(el);

  // 强制重排后加 class，触发过渡
  requestAnimationFrame(() => el.classList.add('is-in'));

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    el.classList.remove('is-in');
    setTimeout(() => el.remove(), 220);
  };
  el.addEventListener('click', close);
  if (duration > 0) setTimeout(close, duration);
  return close;
}
