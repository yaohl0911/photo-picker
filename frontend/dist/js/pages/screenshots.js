// 截图清理页：识别结果浏览 + 批量淘汰。
//
// 首次进入触发后端懒分析（可能数秒）；统计条 + 时间筛选 + 网格卡片
// （点选）+ 分页 + 吸底操作条批量淘汰（走决策管线，可撤销）。

import { getScreenshots, rejectScreenshots, thumbUrl } from '../api.js';
import { toast, fmtNumber, fmtBytes, fmtDateTime, noValue } from '../util.js';

const WHEN_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: '30d', label: '最近 30 天' },
  { value: '2025', label: '2025' },
  { value: 'older', label: '更早' },
];
const PAGE_SIZE_OPTIONS = [60, 120, 240];
let state = {
  when: 'all',
  offset: 0,
  pageSize: 60,
  total: 0,
  items: [],
  stats: null,
  selected: new Set(),
  busy: false,
};

function buildSkeleton(container) {
  container.replaceChildren();
  const wrap = document.createElement('div');
  wrap.className = 'ss-page';
  wrap.innerHTML = `
    <div class="page-head">
      <h1>截图清理</h1>
      <p class="page-head__sub">基于屏幕分辨率与文件特征识别的截图，批量淘汰后进入淘汰目录，可随时撤销。</p>
    </div>
    <div class="ss-loading" data-testid="ss-loading">正在分析照片库（首次可能需要数秒）…</div>`;
  container.appendChild(wrap);
  return wrap;
}

function buildLayout(wrap) {
  wrap.innerHTML = `
    <div class="page-head">
      <h1>截图清理</h1>
      <p class="page-head__sub">基于屏幕分辨率与文件特征识别的截图，批量淘汰后进入淘汰目录，可随时撤销。</p>
    </div>
    <div class="ss-stats" id="ss-stats" data-testid="ss-stats"></div>
    <div class="ss-toolbar">
      <div class="filter-chips" id="ss-filters" data-testid="ss-filters"></div>
      <div class="ss-select-all">
        <button class="btn btn--ghost" id="ss-check-all" data-testid="ss-check-all">全选本页</button>
        <button class="btn btn--ghost" id="ss-check-none" data-testid="ss-check-none">全不选</button>
      </div>
    </div>
    <div class="ss-grid" id="ss-grid" data-testid="ss-grid"></div>
    <div class="pager" id="ss-pager" data-testid="ss-pager"></div>
    <div class="ss-empty" id="ss-empty" hidden>
      <p class="ss-empty__icon" aria-hidden="true">🖥</p>
      <p>当前筛选条件下没有截图。</p>
      <a class="btn btn--ghost" href="#/overview">返回总览</a>
    </div>`;
  document.getElementById('ss-check-all').addEventListener('click', () => {
    state.items.forEach((item) => state.selected.add(item.photo_id));
    renderGrid(); renderActionbar(); renderPager();
  });
  document.getElementById('ss-check-none').addEventListener('click', () => {
    state.selected.clear();
    renderGrid(); renderActionbar(); renderPager();
  });
}

function renderStats(stats) {
  const el = document.getElementById('ss-stats');
  if (!el || !stats) return;
  const cards = [
    { label: '截图数量', value: fmtNumber(stats.screenshots) + ' 张', testid: 'ss-count' },
    { label: '占照片总数', value: stats.percent + '%' },
    { label: '可释放空间', value: fmtBytes(stats.bytes), testid: 'ss-bytes' },
    { label: '最近一张', value: stats.latest ? fmtDateTime(stats.latest) : noValue },
  ];
  el.replaceChildren(...cards.map((c) => {
    const card = document.createElement('div');
    card.className = 'ss-stat';
    if (c.testid) card.dataset.testid = c.testid;
    const label = document.createElement('span');
    label.className = 'ss-stat__label';
    label.textContent = c.label;
    const value = document.createElement('strong');
    value.className = 'ss-stat__value';
    value.textContent = c.value;
    card.append(label, value);
    return card;
  }));
}

function renderFilters() {
  const el = document.getElementById('ss-filters');
  if (!el) return;
  el.replaceChildren(...WHEN_OPTIONS.map((opt) => {
    const chip = document.createElement('button');
    chip.className = 'filter-chip' + (state.when === opt.value ? ' filter-chip--active' : '');
    chip.textContent = opt.label;
    chip.dataset.value = opt.value;
    chip.addEventListener('click', () => {
      if (state.busy || state.when === opt.value) return;
      state.when = opt.value;
      state.offset = 0;
      state.selected.clear();
      load();
    });
    return chip;
  }));
}

function renderGrid() {
  const el = document.getElementById('ss-grid');
  if (!el) return;
  el.replaceChildren(...state.items.map((item) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'ss-card' + (state.selected.has(item.photo_id) ? ' is-selected' : '');
    card.dataset.photoId = String(item.photo_id);
    card.dataset.testid = 'ss-card';
    card.setAttribute('aria-pressed', state.selected.has(item.photo_id) ? 'true' : 'false');

    const img = document.createElement('img');
    img.className = 'ss-card__img';
    img.loading = 'lazy';
    img.src = thumbUrl(item.file_id, 256);
    img.alt = item.name;

    const meta = document.createElement('span');
    meta.className = 'ss-card__meta';
    const name = document.createElement('span');
    name.className = 'ss-card__name';
    name.textContent = item.name;
    name.title = item.name;
    const dims = document.createElement('span');
    dims.className = 'ss-card__dims';
    dims.textContent = item.width && item.height
      ? `${item.width}×${item.height}` : noValue;
    const why = document.createElement('span');
    why.className = 'ss-card__why';
    why.textContent = item.why || '截图';
    meta.append(name, dims, why);

    const check = document.createElement('span');
    check.className = 'ss-card__check';
    check.textContent = '✓';
    check.setAttribute('aria-hidden', 'true');

    card.append(img, meta, check);
    card.addEventListener('click', () => {
      const id = item.photo_id;
      if (state.selected.has(id)) state.selected.delete(id);
      else state.selected.add(id);
      card.classList.toggle('is-selected', state.selected.has(id));
      card.setAttribute('aria-pressed', state.selected.has(id) ? 'true' : 'false');
      renderActionbar();
      renderPager();
    });
    return card;
  }));
}

function renderPager() {
  const el = document.getElementById('ss-pager');
  if (!el) return;
  const pageSize = state.pageSize;
  const totalPages = Math.max(1, Math.ceil(state.total / pageSize));
  const page = Math.floor(state.offset / pageSize) + 1;
  const from = state.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + pageSize, state.total);

  el.replaceChildren();

  const sizeSel = document.createElement('select');
  sizeSel.className = 'pager__page-size';
  sizeSel.dataset.testid = 'ss-page-size';
  sizeSel.setAttribute('aria-label', '每页显示数量');
  PAGE_SIZE_OPTIONS.forEach((n) => {
    const opt = document.createElement('option');
    opt.value = String(n);
    opt.textContent = `${n} 条/页`;
    if (n === pageSize) opt.selected = true;
    sizeSel.appendChild(opt);
  });
  sizeSel.addEventListener('change', () => {
    state.pageSize = Number(sizeSel.value);
    state.offset = 0;
    state.selected.clear();
    load();
  });

  const prev = document.createElement('button');
  prev.className = 'btn btn--ghost';
  prev.textContent = '上一页';
  prev.disabled = page <= 1;
  prev.addEventListener('click', () => gotoPage(page - 1));

  const info = document.createElement('span');
  info.className = 'pager__info';
  info.textContent = `${from}–${to} / ${fmtNumber(state.total)} 张`;

  const next = document.createElement('button');
  next.className = 'btn btn--ghost';
  next.textContent = '下一页';
  next.disabled = page >= totalPages;
  next.addEventListener('click', () => gotoPage(page + 1));

  const jump = document.createElement('span');
  jump.className = 'pager__jump';
  const jumpInput = document.createElement('input');
  jumpInput.type = 'number';
  jumpInput.min = '1';
  jumpInput.max = String(totalPages);
  jumpInput.value = String(page);
  jumpInput.dataset.testid = 'ss-jump';
  jumpInput.setAttribute('aria-label', '跳转到页码');
  const jumpBtn = document.createElement('button');
  jumpBtn.className = 'btn btn--ghost';
  jumpBtn.textContent = '跳转';
  const doJump = () => {
    const target = parseInt(jumpInput.value, 10);
    if (!Number.isNaN(target)) gotoPage(target);
  };
  jumpBtn.addEventListener('click', doJump);
  jumpInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); doJump(); }
  });
  jump.append(jumpInput, jumpBtn);

  const selectedInfo = document.createElement('span');
  selectedInfo.className = 'pager__info';
  selectedInfo.dataset.testid = 'ss-selected-count';
  selectedInfo.textContent = `已选 ${state.selected.size} 张`;
  el.append(sizeSel, prev, info, next, jump, selectedInfo);
}

function gotoPage(page) {
  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  const clamped = Math.min(Math.max(1, page), totalPages);
  state.offset = (clamped - 1) * state.pageSize;
  state.selected.clear();
  load();
}

function renderActionbar() {
  let bar = document.querySelector('.ss-actionbar');
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'ss-actionbar';
    bar.dataset.testid = 'ss-actionbar';
    bar.innerHTML = `
      <span class="ss-actionbar__info">
        已选 <strong data-testid="ss-actionbar-count">0</strong> 张
        <span class="ss-actionbar__hint">移入 <code>_photo_picker_rejected</code>，可撤销，不真删</span>
      </span>
      <button class="btn btn--danger" data-testid="ss-reject-btn">批量淘汰</button>`;
    const btn = bar.querySelector('[data-testid="ss-reject-btn"]');
    btn.addEventListener('click', onReject);
    document.body.appendChild(bar);
  }
  const count = bar.querySelector('[data-testid="ss-actionbar-count"]');
  if (count) count.textContent = String(state.selected.size);
  const btn = bar.querySelector('[data-testid="ss-reject-btn"]');
  if (btn) {
    btn.disabled = state.busy || state.selected.size === 0;
    btn.textContent = state.selected.size > 0 ? `批量淘汰 ${state.selected.size} 张` : '批量淘汰';
  }
}

async function onReject() {
  if (state.busy || state.selected.size === 0) return;
  state.busy = true;
  renderActionbar();
  try {
    const result = await rejectScreenshots([...state.selected]);
    toast(`已淘汰 ${result.moved_files} 个文件，可在「历史」页撤销`, { kind: 'ok' });
    state.selected.clear();
    state.offset = 0;
    await load();
  } catch (err) {
    toast(`淘汰失败：${err.message}`, { kind: 'error' });
    state.busy = false;
    renderActionbar();
  }
}

async function load() {
  state.busy = true;
  renderActionbar();
  const grid = document.getElementById('ss-grid');
  if (grid) {
    grid.innerHTML = '<div class="ss-loading" data-testid="ss-loading">加载中…</div>';
  }
  try {
    const data = await getScreenshots({
      offset: state.offset, limit: state.pageSize, when: state.when,
    });
    state.items = data.items || [];
    state.total = data.total || 0;
    state.stats = data.stats || null;
    state.busy = false;
    const empty = document.getElementById('ss-empty');
    if (empty) empty.hidden = state.total > 0;
    renderStats(state.stats);
    renderFilters();
    renderGrid();
    renderPager();
    renderActionbar();
  } catch (err) {
    state.busy = false;
    const wrap = document.querySelector('.ss-page');
    if (wrap) {
      wrap.innerHTML = `
        <div class="page-head"><h1>截图清理</h1></div>
        <p class="form-error" data-testid="ss-error">加载失败：${err.message}</p>`;
    }
    renderActionbar();
  }
}

export function renderScreenshots(container) {
  const wrap = buildSkeleton(container);
  buildLayout(wrap);
  load();
  return function cleanup() {
    const bar = document.querySelector('.ss-actionbar');
    if (bar) bar.remove();
    state = {
      when: 'all', offset: 0, pageSize: 60, total: 0,
      items: [], stats: null, selected: new Set(), busy: false,
    };
  };
}
