// 决策历史页（Task 12）。
//
// 路由：#/history。
//
// 结构：
//   - 页头（总决策数统计）
//   - 决策记录列表：每行 = 组号（链接 #/compare?group={id}）+ 组状态徽章
//     + 保留 N 张 + decided_at + moved_files 已移数 + undone「已撤销」红徽章
//     / 未撤销行「撤销」按钮（POST /api/groups/{id}/undo）
//   - 行可展开（lazy）：点行首展开符号时才请求 getGroup(group_id)，
//     渲染 kept_photo_ids 对应照片缩略图（thumb_url）。
//     选择「行内展开」而非跳转 compare：历史浏览是回溯场景，展开可
//     保持分页与滚动状态、不打断浏览流；组号链接仍提供完整跳转。
//   - 分页 offset/limit 20 + total 计数；空态「还没有决策记录」。

import { listHistory, undoGroup, getGroup } from '../api.js';
import { toast, fmtNumber, fmtDateTime } from '../util.js';

const PAGE_SIZE = 20;

export function renderHistory(container) {
  const section = document.createElement('section');
  section.className = 'page page--history';
  container.appendChild(section);

  let disposed = false;
  let busy = false;
  let offset = 0;
  let total = 0;
  let decisions = [];
  const expanded = new Set(); // 已展开（或已加载缩略图）的 group_id

  buildLoading();
  load().catch((err) => {
    if (!disposed) buildError(err);
  });

  async function load() {
    const data = await listHistory({ offset, limit: PAGE_SIZE });
    if (disposed) return;
    decisions = data.decisions || [];
    total = data.total || 0;
    expanded.clear();
    build();
  }

  async function reload() {
    // 撤销等写操作后的刷新（保持当前 offset 与滚动位置；若当前页被
    // 刷空且不是第一页，则回退一页重取）。
    const y = window.scrollY;
    try {
      await load();
      if (disposed) return;
      if (!decisions.length && offset > 0) {
        offset = Math.max(0, offset - PAGE_SIZE);
        await load();
        if (disposed) return;
      }
      window.scrollTo({ top: y, behavior: 'auto' });
    } catch (err) {
      if (!disposed) toast(`历史刷新失败：${err.message}`, { kind: 'error' });
    }
  }

  /* ---------- 渲染骨架 ---------- */

  function buildLoading() {
    section.innerHTML = '<div class="loading-card" data-testid="loading">加载决策历史中…</div>';
  }

  function buildError(err) {
    section.innerHTML = `
      <div class="empty-state" data-testid="load-error">
        <div class="empty-state__icon" aria-hidden="true">⚠️</div>
        <h2 class="empty-state__title">决策历史加载失败</h2>
        <p class="empty-state__text"></p>
        <div class="empty-state__actions">
          <button type="button" class="btn btn--primary" data-testid="retry">重试</button>
        </div>
      </div>`;
    section.querySelector('.empty-state__text').textContent = String(
      err && err.message ? err.message : err,
    );
    section.querySelector('[data-testid="retry"]').addEventListener('click', () => {
      buildLoading();
      load().catch((e2) => {
        if (!disposed) buildError(e2);
      });
    });
  }

  function build() {
    section.replaceChildren();

    // ---- 页头 ----
    const head = document.createElement('header');
    head.className = 'page__head';
    head.innerHTML = `
      <div>
        <h1 class="page__title">决策历史</h1>
        <p class="page__subtitle">已作出的每组照片决策，可展开查看保留照片，误判可撤销。</p>
      </div>
      <span class="badge badge--count" data-testid="history-total"></span>`;
    head.querySelector('[data-testid="history-total"]').textContent =
      `共 ${fmtNumber(total)} 条决策`;
    section.appendChild(head);

    // ---- 空态 ----
    if (!decisions.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.dataset.testid = 'history-empty';
      empty.innerHTML = `
        <div class="empty-state__icon" aria-hidden="true">🗂️</div>
        <h2 class="empty-state__title">还没有决策记录</h2>
        <p class="empty-state__text">在对比页完成几组照片决策后，这里会记录每组的选择与移动结果。</p>
        <div class="empty-state__actions">
          <a class="btn btn--primary" href="#/compare" data-testid="history-go-compare">去对比决策</a>
        </div>`;
      section.appendChild(empty);
      return;
    }

    // ---- 列表 ----
    const list = document.createElement('div');
    list.className = 'history-list';
    list.dataset.testid = 'history-list';
    decisions.forEach((d, i) => list.appendChild(buildRow(d, i)));
    section.appendChild(list);

    // ---- 分页 ----
    section.appendChild(buildPager());
  }

  /** 单条决策行（含展开区与撤销按钮）。 */
  function buildRow(d, index) {
    const row = document.createElement('article');
    row.className = 'history-row' + (d.undone ? ' is-undone' : '');
    row.dataset.testid = 'history-row';
    row.dataset.groupId = String(d.group_id);
    row.dataset.decisionId = String(d.decision_id);

    // 主行（div[role=button]：内含组号链接与撤销按钮，button 嵌 a 属非法
    // 交互内容嵌套，故用 div + tabindex 支持键盘）
    const main = document.createElement('div');
    main.className = 'history-row__main';
    main.dataset.testid = 'history-row-main';
    main.setAttribute('role', 'button');
    main.setAttribute('tabindex', '0');
    main.setAttribute('aria-expanded', 'false');

    const toggle = document.createElement('span');
    toggle.className = 'history-row__toggle';
    toggle.textContent = '▸';
    toggle.setAttribute('aria-hidden', 'true');

    const groupLink = document.createElement('a');
    groupLink.className = 'history-row__group';
    groupLink.href = `#/compare?group=${d.group_id}`;
    groupLink.dataset.testid = 'history-group-link';
    groupLink.textContent = `组 ${d.group_id}`;
    groupLink.title = '在对比页查看该组';
    groupLink.addEventListener('click', (ev) => ev.stopPropagation());

    const status = document.createElement('span');
    status.className = `badge badge--status-${d.group_status || 'decided'}`;
    status.textContent = statusText(d.group_status);

    const kept = document.createElement('span');
    kept.className = 'history-row__kept';
    kept.dataset.testid = 'history-kept';
    kept.textContent = d.kind === 'dismiss'
      ? '判定非重复'
      : `保留 ${(d.kept_photo_ids || []).length} 张`;

    const time = document.createElement('span');
    time.className = 'history-row__time';
    time.dataset.testid = 'history-decided-at';
    time.textContent = d.decided_at ? fmtDateTime(d.decided_at) : '—';
    time.title = d.applied_at ? `应用时间：${fmtDateTime(d.applied_at)}` : '';

    const moved = document.createElement('span');
    moved.className = 'history-row__moved';
    moved.dataset.testid = 'history-moved';
    // 后端契约：moved_files 为 move_log 行数（int）；数组形式兼容旧 mock。
    const movedCount = Array.isArray(d.moved_files)
      ? d.moved_files.length
      : Number(d.moved_files) || 0;
    moved.textContent = movedCount > 0 ? `${movedCount} 个文件已移` : '未移动文件';

    main.append(toggle, groupLink, status, kept, time, moved);

    // 右侧状态区：已撤销徽章 / 撤销按钮
    const right = document.createElement('span');
    right.className = 'history-row__right';
    if (d.undone) {
      const undoneBadge = document.createElement('span');
      undoneBadge.className = 'badge badge--undone';
      undoneBadge.dataset.testid = 'undone-badge';
      undoneBadge.textContent = '已撤销';
      right.appendChild(undoneBadge);
    } else {
      const undoBtn = document.createElement('button');
      undoBtn.type = 'button';
      undoBtn.className = 'btn btn--sm btn--ghost';
      undoBtn.dataset.testid = 'undo-btn';
      undoBtn.textContent = '撤销';
      undoBtn.title = '撤销该组最新决策，恢复被移走的文件';
      undoBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        undoDecision(d, row);
      });
      right.appendChild(undoBtn);
    }

    main.appendChild(right);
    main.addEventListener('click', () => toggleRow(d, row, main));
    main.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        toggleRow(d, row, main);
      }
    });
    row.appendChild(main);

    // 展开区（首次展开时 lazy 填充）
    const detail = document.createElement('div');
    detail.className = 'history-row__detail';
    detail.dataset.testid = 'history-detail';
    row.appendChild(detail);
    return row;
  }

  /** 展开/收起；首次展开 lazy 请求 getGroup 渲染保留照片缩略图。 */
  function toggleRow(d, row, main) {
    const detail = row.querySelector('.history-row__detail');
    if (!detail) return;
    const open = row.classList.toggle('is-open');
    main.setAttribute('aria-expanded', String(open));
    row.querySelector('.history-row__toggle').textContent = open ? '▾' : '▸';
    if (open && !expanded.has(d.group_id)) {
      expanded.add(d.group_id);
      renderKeptThumbs(d, detail);
    }
  }

  /** lazy 拉取组数据并渲染 kept_photo_ids 的缩略图。 */
  async function renderKeptThumbs(d, detail) {
    const holder = document.createElement('div');
    holder.className = 'history-thumbs';
    holder.dataset.testid = 'history-thumbs';
    detail.appendChild(holder);
    holder.innerHTML = '<div class="loading-card" data-testid="thumbs-loading">加载保留照片中…</div>';
    try {
      const group = await getGroup(d.group_id);
      if (disposed || !holder.isConnected) return;
      const keptIds = new Set(d.kept_photo_ids || []);
      // dismiss 决策没有保留集——展示整组照片帮用户回忆当时为何判非重复
      const photos = d.kind === 'dismiss'
        ? (group.photos || [])
        : (group.photos || []).filter((p) => keptIds.has(p.photo_id));
      holder.replaceChildren();
      if (!photos.length) {
        holder.className += ' history-thumbs--empty';
        holder.textContent = '该组保留的照片信息已不可用';
        return;
      }
      for (const p of photos) {
        const thumb = document.createElement('img');
        thumb.className = 'group-card__thumb';
        thumb.dataset.testid = 'history-thumb';
        thumb.src = p.thumb_url || '';
        thumb.alt = `保留照片 ${p.photo_id}`;
        thumb.loading = 'lazy';
        thumb.title = `photo #${p.photo_id}${p.is_recommended ? '（推荐）' : ''}`;
        holder.appendChild(thumb);
      }
      const note = document.createElement('span');
      note.className = 'history-thumbs__note';
      const movedNote = Array.isArray(d.moved_files)
        ? d.moved_files.length
        : Number(d.moved_files) || 0;
      note.textContent = d.kind === 'dismiss'
        ? `非重复 · 组内 ${photos.length} 张照片`
        : `保留 ${photos.length} 张 · 已移 ${movedNote} 个文件`;
      holder.appendChild(note);
    } catch (err) {
      if (disposed || !holder.isConnected) return;
      holder.replaceChildren();
      holder.className += ' history-thumbs--empty';
      holder.textContent = `缩略图加载失败：${err && err.message ? err.message : err}`;
    }
  }

  /** 撤销该组最新决策：POST undo → toast 恢复数 → 刷新当前列表。 */
  async function undoDecision(d, row) {
    if (busy) {
      toast('有操作正在进行中，请稍候', { kind: 'info' });
      return;
    }
    busy = true;
    const btn = row.querySelector('[data-testid="undo-btn"]');
    if (btn) {
      btn.classList.add('is-loading');
      btn.disabled = true;
      btn.textContent = '撤销中…';
    }
    try {
      const res = await undoGroup(d.group_id);
      if (disposed) return;
      const restored = res && res.restored_files != null ? res.restored_files : 0;
      toast(
        `已撤销组 ${d.group_id} 的最新决策${restored > 0 ? `，恢复 ${fmtNumber(restored)} 个文件` : ''}`,
        { kind: 'ok' },
      );
      await reload();
    } catch (err) {
      if (!disposed) toast(`撤销失败：${err.message}`, { kind: 'error' });
      if (btn && btn.isConnected) {
        btn.classList.remove('is-loading');
        btn.disabled = false;
        btn.textContent = '撤销';
      }
    } finally {
      if (!disposed) busy = false;
    }
  }

  /* ---------- 分页 ---------- */

  function buildPager() {
    const page = Math.floor(offset / PAGE_SIZE) + 1;
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const from = total === 0 ? 0 : offset + 1;
    const to = Math.min(offset + decisions.length, total);

    const pager = document.createElement('nav');
    pager.className = 'pager';
    pager.dataset.testid = 'history-pager';
    pager.innerHTML = `
      <button type="button" class="btn btn--pager" data-testid="pager-prev" ${offset === 0 ? 'disabled' : ''}>上一页</button>
      <span class="pager__info" data-testid="pager-info"></span>
      <button type="button" class="btn btn--pager" data-testid="pager-next" ${offset + PAGE_SIZE >= total ? 'disabled' : ''}>下一页</button>`;
    pager.querySelector('[data-testid="pager-info"]').textContent =
      `第 ${page}/${pages} 页 · ${fmtNumber(from)}–${fmtNumber(to)} / ${fmtNumber(total)} 条`;

    pager.querySelector('[data-testid="pager-prev"]').addEventListener('click', () => {
      offset = Math.max(0, offset - PAGE_SIZE);
      buildLoading();
      load().catch((e) => {
        if (!disposed) buildError(e);
      });
    });
    pager.querySelector('[data-testid="pager-next"]').addEventListener('click', () => {
      if (offset + PAGE_SIZE < total) offset += PAGE_SIZE;
      buildLoading();
      load().catch((e) => {
        if (!disposed) buildError(e);
      });
    });
    return pager;
  }

  /* ---------- 工具 ---------- */

  function statusText(s) {
    if (s === 'pending') return '待决策';
    if (s === 'decided') return '已决策';
    if (s === 'not_duplicate') return '非重复';
    return s || '—';
  }

  /* ---------- cleanup ---------- */

  return () => {
    disposed = true;
  };
}
