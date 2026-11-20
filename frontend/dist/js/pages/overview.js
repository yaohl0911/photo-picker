// 总览页：库统计 + 重复组卡片流。
//
// 数据：getOverview()（统计）+ listGroups({status, offset, limit})（分页卡片）。
// 三态 tab：pending（默认）/ decided / not_duplicate，切换重新拉取并回到第一页。
// 卡片：组内前 4 张缩略图 + 推荐高亮（★）+ 推荐理由（2 行截断，title 全文）
//       + photo_count 徽章 + 「去决策」按钮 → #/compare?group={id}。

import { getOverview, listGroups, thumbUrl } from '../api.js';
import { toast, fmtNumber } from '../util.js';

const PAGE_SIZE_OPTIONS = [20, 50, 100, 200];
const DEFAULT_PAGE_SIZE = 50;

const STATUSES = [
  { key: 'pending', label: '待决策' },
  { key: 'decided', label: '已决策' },
  { key: 'not_duplicate', label: '非重复' },
];

const STATUS_LABEL = Object.fromEntries(STATUSES.map((s) => [s.key, s.label]));

export function renderOverview(container) {
  const section = document.createElement('section');
  section.className = 'page page--overview';
  section.innerHTML = `
    <header class="page__head">
      <div>
        <h1 class="page__title">总览</h1>
        <p class="page__subtitle">按推荐顺序处理重复组，每次决策都会用于学习你的偏好。</p>
      </div>
    </header>
    <div class="stat-strip" data-testid="stat-strip" aria-label="库统计"></div>
    <div class="overview-toolbar">
      <div class="tabs" role="tablist" data-testid="status-tabs"></div>
    </div>
    <div class="group-list" data-testid="group-list"></div>
    <nav class="pager" data-testid="pager" aria-label="分页"></nav>
  `;
  container.appendChild(section);

  const $ = (sel) => section.querySelector(sel);
  const tabsEl = $('[data-testid="status-tabs"]');
  const listEl = $('[data-testid="group-list"]');
  const pagerEl = $('[data-testid="pager"]');
  const stripEl = $('[data-testid="stat-strip"]');

  const state = {
  status: 'pending', offset: 0, total: 0,
  pageSize: DEFAULT_PAGE_SIZE, // 每页展示组数（可选）
};
  let disposed = false;

  /* ---------- 统计条 ---------- */

  function renderStats(stats) {
    const chips = [
      { label: '照片', value: stats.total_photos },
      { label: '文件', value: stats.total_files },
      { label: '待决策组', value: stats.pending_groups },
      { label: '已决策', value: stats.decided_groups },
      { label: '非重复', value: stats.not_duplicate_groups },
      { label: '决策次数', value: stats.decisions_count },
      { label: '自动去重', value: stats.auto_resolved_groups },
      { label: '学习样本', value: stats.samples },
    ];
    stripEl.replaceChildren(
      ...chips.map((c) => {
        const chip = document.createElement('div');
        chip.className = 'stat-chip';
        const v = document.createElement('span');
        v.className = 'stat-chip__value';
        v.textContent = fmtNumber(c.value);
        const l = document.createElement('span');
        l.className = 'stat-chip__label';
        l.textContent = c.label;
        chip.append(v, l);
        return chip;
      }),
    );
    // 喂 tab 徽章
    const counts = {
      pending: stats.pending_groups,
      decided: stats.decided_groups,
      not_duplicate: stats.not_duplicate_groups,
    };
    tabsEl.querySelectorAll('.tab').forEach((btn) => {
      const span = btn.querySelector('.tab__count');
      if (span) span.textContent = fmtNumber(counts[btn.dataset.status]);
    });
  }

  async function loadStats() {
    try {
      const stats = await getOverview();
      if (disposed) return;
      renderStats(stats);
    } catch (err) {
      if (!disposed) toast(`统计加载失败：${err.message}`, { kind: 'error' });
    }
  }

  /* ---------- tabs ---------- */

  for (const s of STATUSES) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tab';
    btn.dataset.status = s.key;
    btn.setAttribute('role', 'tab');
    btn.innerHTML = `<span>${s.label}</span><span class="tab__count" data-testid="tab-count-${s.key}">—</span>`;
    btn.addEventListener('click', () => {
      if (state.status === s.key) return;
      state.status = s.key;
      state.offset = 0;
      syncTabs();
      loadGroups();
    });
    tabsEl.appendChild(btn);
  }

  function syncTabs() {
    tabsEl.querySelectorAll('.tab').forEach((btn) => {
      const active = btn.dataset.status === state.status;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });
  }

  /* ---------- 组卡片流 ---------- */

  function thumbImg(photo, isRecommended) {
    const img = document.createElement('img');
    img.className = 'group-card__thumb' + (isRecommended ? ' is-recommended' : '');
    img.alt = `照片 ${photo.photo_id}`;
    img.loading = 'lazy';
    img.src = thumbUrl(photo.primary_file_id, 128);
    img.title = isRecommended ? '推荐保留' : `照片 ${photo.photo_id}`;
    return img;
  }

  function groupCard(g) {
    const card = document.createElement('article');
    card.className = `group-card group-card--${g.status}`;
    card.dataset.groupId = String(g.group_id);
    card.setAttribute('data-testid', 'group-card');

    // 缩略图（前 4 张，推荐者高亮描边 + 星标）
    const thumbs = document.createElement('div');
    thumbs.className = 'group-card__thumbs';
    const shown = (g.photos || []).slice(0, 4);
    for (const p of shown) {
      const isRec = p.photo_id === g.recommended_photo_id;
      thumbs.appendChild(thumbImg(p, isRec));
    }
    if (!shown.length) {
      const empty = document.createElement('div');
      empty.className = 'group-card__thumbs group-card__thumbs--empty';
      empty.textContent = '无预览';
      thumbs.replaceWith(empty);
    }
    card.appendChild(thumbs);

    // 元信息行：组号 + 状态 + 张数徽章
    const meta = document.createElement('div');
    meta.className = 'group-card__meta';
    const statusBadge = document.createElement('span');
    statusBadge.className = `badge badge--status badge--status-${g.status}`;
    statusBadge.textContent = STATUS_LABEL[g.status] || g.status;
    const countBadge = document.createElement('span');
    countBadge.className = 'badge badge--count';
    countBadge.textContent = `${fmtNumber(g.photo_count)} 张`;
    meta.append(statusBadge, countBadge);
    card.appendChild(meta);

    // 推荐理由（pending 有值；decided/not_duplicate 为空串则不渲染）
    if (g.reason) {
      const reason = document.createElement('p');
      reason.className = 'group-card__reason';
      reason.textContent = g.reason;
      reason.title = g.reason; // 悬停看全文
      reason.dataset.testid = 'group-reason';
      card.appendChild(reason);
    }

    // 操作
    const foot = document.createElement('div');
    foot.className = 'group-card__foot';
    const link = document.createElement('a');
    link.className = 'btn btn--primary';
    link.href = `#/compare?group=${g.group_id}`;
    link.textContent = state.status === 'pending' ? '去决策' : '查看对比';
    link.dataset.testid = 'go-compare';
    foot.appendChild(link);
    card.appendChild(foot);

    return card;
  }

  function renderEmpty() {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    if (state.status === 'pending') {
      empty.innerHTML = `
        <div class="empty-state__icon" aria-hidden="true">🗂️</div>
        <h2 class="empty-state__title">还没有待决策的重复组</h2>
        <p class="empty-state__text">先在设置页运行扫描，找出重复照片后再来这里决策。</p>
        <a class="btn btn--primary" href="#/settings">前往设置页扫描</a>`;
    } else {
      empty.innerHTML = `
        <div class="empty-state__icon" aria-hidden="true">✅</div>
        <h2 class="empty-state__title">「${STATUS_LABEL[state.status]}」分类下暂无分组</h2>
        <p class="empty-state__text">切换上方的标签查看其他分组。</p>`;
    }
    return empty;
  }

  async function loadGroups() {
    listEl.replaceChildren(loadingCard());
    pagerEl.replaceChildren();
    try {
      const data = await listGroups({
        status: state.status,
        offset: state.offset,
        limit: state.pageSize,
      });
      if (disposed) return;
      state.total = data.total;
      const frag = document.createDocumentFragment();
      if (!data.groups.length) {
        frag.appendChild(renderEmpty());
      } else {
        for (const g of data.groups) frag.appendChild(groupCard(g));
      }
      listEl.replaceChildren(frag);
      renderPager();
    } catch (err) {
      if (disposed) return;
      listEl.replaceChildren(errorCard(err));
    }
  }

  function loadingCard() {
    const el = document.createElement('div');
    el.className = 'loading-card';
    el.dataset.testid = 'loading';
    el.textContent = '加载分组中…';
    return el;
  }

  function errorCard(err) {
    const el = document.createElement('div');
    el.className = 'loading-card loading-card--error';
    el.dataset.testid = 'load-error';
    const msg = document.createElement('span');
    msg.textContent = `分组加载失败：${err.message}`;
    const retry = document.createElement('button');
    retry.className = 'btn';
    retry.type = 'button';
    retry.textContent = '重试';
    retry.addEventListener('click', loadGroups);
    el.append(msg, retry);
    return el;
  }

  /* ---------- 分页 ---------- */

  function totalPages() {
    return Math.max(1, Math.ceil(state.total / state.pageSize));
  }

  function gotoPage(page) {
    const p = Math.min(Math.max(1, page), totalPages());
    state.offset = (p - 1) * state.pageSize;
    loadGroups();
    scrollToTop();
  }

  function renderPager() {
    const from = state.total === 0 ? 0 : state.offset + 1;
    const to = Math.min(state.offset + state.pageSize, state.total);
    const currentPage = Math.floor(state.offset / state.pageSize) + 1;
    pagerEl.replaceChildren();

    const prev = document.createElement('button');
    prev.type = 'button';
    prev.className = 'btn btn--pager';
    prev.textContent = '上一页';
    prev.disabled = state.offset <= 0;
    prev.addEventListener('click', () => {
      if (state.offset <= 0) return;
      state.offset = Math.max(0, state.offset - state.pageSize);
      loadGroups();
      scrollToTop();
    });

    const info = document.createElement('span');
    info.className = 'pager__info';
    info.dataset.testid = 'pager-info';
    info.textContent = `第 ${from}–${to} 组 · 共 ${fmtNumber(state.total)} 组`;

    const next = document.createElement('button');
    next.type = 'button';
    next.className = 'btn btn--pager';
    next.textContent = '下一页';
    next.disabled = to >= state.total;
    next.addEventListener('click', () => {
      if (to >= state.total) return;
      state.offset += state.pageSize;
      loadGroups();
      scrollToTop();
    });

    // 每页展示组数：切换后回到第 1 页
    const sizeSel = document.createElement('select');
    sizeSel.className = 'pager__page-size';
    sizeSel.dataset.testid = 'page-size';
    sizeSel.setAttribute('aria-label', '每页展示组数');
    for (const n of PAGE_SIZE_OPTIONS) {
      const opt = document.createElement('option');
      opt.value = String(n);
      opt.textContent = `每页 ${n} 组`;
      if (n === state.pageSize) opt.selected = true;
      sizeSel.appendChild(opt);
    }
    sizeSel.addEventListener('change', () => {
      const n = Number(sizeSel.value) || DEFAULT_PAGE_SIZE;
      if (n === state.pageSize) return;
      state.pageSize = n;
      state.offset = 0;
      loadGroups();
      scrollToTop();
    });

    // 直接跳转到第几页
    const jump = document.createElement('span');
    jump.className = 'pager__jump';
    const jumpInput = document.createElement('input');
    jumpInput.type = 'number';
    jumpInput.className = 'pager__jump-input';
    jumpInput.dataset.testid = 'jump-page';
    jumpInput.min = '1';
    jumpInput.max = String(totalPages());
    jumpInput.value = String(currentPage);
    jumpInput.setAttribute('aria-label', '跳转到页码');
    const jumpBtn = document.createElement('button');
    jumpBtn.type = 'button';
    jumpBtn.className = 'btn btn--pager';
    jumpBtn.dataset.testid = 'jump-btn';
    jumpBtn.textContent = '跳转';
    const doJump = () => {
      const p = parseInt(jumpInput.value, 10);
      if (!Number.isFinite(p)) return;
      gotoPage(p);
    };
    jumpBtn.addEventListener('click', doJump);
    jumpInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        doJump();
      }
    });
    jump.append(jumpInput, jumpBtn);

    pagerEl.append(sizeSel, prev, info, next, jump);
  }

  function scrollToTop() {
    window.scrollTo({ top: 0, behavior: 'auto' });
  }

  /* ---------- 启动 ---------- */

  syncTabs();
  loadStats();
  loadGroups();

  return () => {
    disposed = true;
  };
}
