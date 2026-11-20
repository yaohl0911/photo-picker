// 对比决策页 —— 本应用核心交互页。
//
// 路由：#/compare?group={id}（未带 id 时自动跳到第一个待决策组）。
//
// 结构：
//   1. 顶部：组号 + 状态徽章 + 返回总览 + 缩放读数/复位 + 快捷键帮助（?）
//   2. 推荐理由横幅（组级 reason，醒目位置，显示一次）
//   3. SyncViewer 同步对比区：组内照片并排等高展示，拖一张全体同步平移、
//      滚轮同步缩放（光标锚定 0.2x–8x）、双击复位（js/sync-viewer.js）
//   4. 每图信息面板：文件名/格式徽章（RAW 高亮）/拍摄时间/EXIF/五维评分条/反馈
//   5. 吸底决策操作条：确认保留 N 张（learn 开关）/标记非重复/撤销上个决策
//   6. 快捷键：1..9 选择、0 全不选、Enter 确认、D 非重复、←/→ 换组、? 帮助
//
// 会话级缓存（模块级变量，跨组导航保留）：pendingIdsCache（←/→ 与自动跳组用）、
// lastDecisionGroupId（撤销「上个决策」目标优先级 1）。

import {
  getGroup,
  decideGroup,
  dismissGroup,
  undoGroup,
  sendFeedback,
  listGroups,
  listHistory,
  previewUrl,
} from '../api.js';
import { toast, fmtDateTime, fmtExposure, fmtAperture, basename } from '../util.js';
import { createSyncViewer } from '../sync-viewer.js';

/* ---------- 常量 ---------- */

const STATUS_LABEL = { pending: '待决策', decided: '已决策', not_duplicate: '非重复' };

const DIMS = [
  { key: 'sharpness', label: '清晰度' },
  { key: 'exposure', label: '曝光' },
  { key: 'noise', label: '噪点控制' },
  { key: 'composition', label: '构图水平' },
  { key: 'faces', label: '人物状态' },
];

const RAW_EXT_RE = /\.(cr2|cr3|nef|arw|dng|raf|orf|rw2|pef|srw|x3f)$/i;

/* ---------- 会话级缓存 ---------- */

let pendingIdsCache = null; // number[] | null（null = 未加载）
let lastDecisionGroupId = null; // 本会话最近一次决策的组

async function ensurePendingList() {
  if (pendingIdsCache !== null) return pendingIdsCache;
  try {
    const ids = [];
    let offset = 0;
    for (;;) {
      const data = await listGroups({ status: 'pending', offset, limit: 500 });
      for (const g of data.groups) ids.push(g.group_id);
      offset += 500;
      if (offset >= data.total || !data.groups.length) break;
    }
    pendingIdsCache = ids;
  } catch (_) {
    pendingIdsCache = []; // 静默失败：←/→ 仍可用，只是无组可跳
  }
  return pendingIdsCache;
}

function removePendingId(id) {
  if (pendingIdsCache === null) return;
  const i = pendingIdsCache.indexOf(id);
  if (i !== -1) pendingIdsCache.splice(i, 1);
}

function addPendingId(id) {
  if (pendingIdsCache === null || id == null) return;
  if (!pendingIdsCache.includes(id)) {
    pendingIdsCache.push(id);
    pendingIdsCache.sort((a, b) => a - b); // 后端 ORDER BY id，保持同序
  }
}

/** 返回相对 currentId 的相邻 pending 组 id（dir=+1/-1）；无则 null。 */
function neighborPending(currentId, dir) {
  const ids = pendingIdsCache || [];
  const idx = ids.indexOf(currentId);
  if (idx === -1) return dir > 0 ? (ids.length ? ids[0] : null) : null;
  const next = ids[idx + dir];
  return next === undefined ? null : next;
}

/* ---------- 工具 ---------- */

/** 从 hash 提取 group 参数；无/非法返回 null。 */
function groupParamFromHash() {
  const raw = location.hash || '';
  const qIdx = raw.indexOf('?');
  if (qIdx === -1) return null;
  const g = new URLSearchParams(raw.slice(qIdx + 1)).get('group');
  if (g === null || g === '') return null;
  const n = Number(g);
  return Number.isInteger(n) && n > 0 ? n : null;
}

function isRawFile(f) {
  if (!f) return false;
  if (f.is_raw) return true;
  const fmt = (f.format || '').toLowerCase();
  if (fmt === 'raw') return true;
  return RAW_EXT_RE.test(f.path || '');
}

function primaryFile(p) {
  const files = p.files || [];
  return files.find((f) => f.file_id === p.primary_file_id) || files[0] || null;
}

/* ---------- 页面 ---------- */

export function renderCompare(container) {
  const section = document.createElement('section');
  section.className = 'page page--compare';
  container.appendChild(section);

  let disposed = false;
  let group = null; // 组详情
  let photos = null; // photos（按推荐 rank 排序）
  let selected = new Set(); // 选中的 photo_id 集合（默认预选推荐）
  let viewer = null; // SyncViewer 实例
  let busy = false; // 决策请求进行中
  let cheatsheetOpen = false;
  let historyInitGroupId = null; // 入页时从历史初始化的撤销目标（0 = 无历史）
  const feedbackState = new Map(); // photo_id -> 'like' | 'dislike'（会话内）

  // DOM 引用（buildGroup 时赋值）
  let zoomReadoutEl = null;
  let resetBtnEl = null;
  let selectedCountEl = null;
  let confirmBtnEl = null;
  let dismissBtnEl = null;
  let undoBtnEl = null;
  let learnToggleEl = null;
  let cheatsheetEl = null;

  /* ---------- 快捷键 ---------- */

  function onKeydown(e) {
    if (disposed) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return; // 不劫持浏览器/系统快捷键
    const k = e.key;

    if (k === 'Escape') {
      if (cheatsheetOpen) {
        e.preventDefault();
        setCheatsheet(false);
      }
      return;
    }
    if (cheatsheetOpen) {
      if (k === '?' || k === '/') {
        e.preventDefault();
        setCheatsheet(false);
      }
      return; // 帮助弹层打开时其他快捷键不生效
    }
    if (k === '?') {
      e.preventDefault();
      setCheatsheet(true);
      return;
    }
    if (photos === null || group === null) return; // 未加载完成

    if (k >= '1' && k <= '9') {
      const idx = Number(k) - 1;
      if (idx < photos.length) {
        e.preventDefault();
        toggleSelect(photos[idx].photo_id);
      }
      return;
    }
    if (k === '0') {
      e.preventDefault();
      selected.clear();
      updateSelectionUI();
      return;
    }
    if (k === 'Enter') {
      e.preventDefault(); // 防止焦点在按钮上时双触发
      confirmDecision();
      return;
    }
    if (k === 'd' || k === 'D') {
      e.preventDefault();
      dismissCurrent();
      return;
    }
    if (k === 'ArrowLeft') {
      e.preventDefault();
      gotoPending(-1);
      return;
    }
    if (k === 'ArrowRight') {
      e.preventDefault();
      gotoPending(1);
      return;
    }
  }

  window.addEventListener('keydown', onKeydown);

  /* ---------- 入口 ---------- */

  buildLoading();

  init().catch((err) => {
    if (!disposed) buildError(err);
  });

  async function init() {
    const idParam = groupParamFromHash();
    if (idParam === null) {
      // 未带 id：跳第一个 pending 组
      await ensurePendingList();
      if (disposed) return;
      const first = pendingIdsCache[0];
      if (first === undefined) {
        buildNoTarget();
        return;
      }
      location.replace(`#/compare?group=${first}`); // 触发 hashchange → 重新渲染
      return;
    }

    let detail = null;
    let err = null;
    const pendingPromise = idParam !== null ? ensurePendingList() : null; // 并行预取 pending 列表（←/→ 与决策后跳组用）
    try {
      detail = await getGroup(idParam);
    } catch (e) {
      err = e;
    }
    if (pendingPromise) {
      try {
        await pendingPromise;
      } catch (_) {
        /* ensurePendingList 内部已兜底 */
      }
    }
    if (disposed) return;
    if (err) {
      buildError(err);
      return;
    }
    group = detail;
    photos = detail.photos || [];
    // 撤销目标初始化：当前组非 decided 且本会话尚未决策过 → 查历史最新决策
    if (lastDecisionGroupId === null && detail.status !== 'decided') {
      initUndoTargetFromHistory();
    }
    buildGroup();
  }

  async function initUndoTargetFromHistory() {
    try {
      const data = await listHistory({ offset: 0, limit: 1 });
      if (disposed) return;
      const d = data && data.decisions && data.decisions[0];
      historyInitGroupId = d && d.group_id != null ? d.group_id : 0;
    } catch (_) {
      historyInitGroupId = 0;
    }
  }

  /* ---------- 选择交互 ---------- */

  function toggleSelect(photoId) {
    if (selected.has(photoId)) selected.delete(photoId);
    else selected.add(photoId);
    updateSelectionUI();
  }

  function updateSelectionUI() {
    if (photos === null) return;
    const ordered = photos.filter((p) => selected.has(p.photo_id));
    const orderMap = new Map(ordered.map((p, i) => [p.photo_id, i + 1]));
    section.querySelectorAll('.photo-card').forEach((card) => {
      const pid = Number(card.dataset.photoId);
      const on = selected.has(pid);
      card.classList.toggle('is-selected', on);
      const badge = card.querySelector('[data-testid="sel-badge"]');
      if (badge) {
        badge.hidden = !on;
        if (on) badge.textContent = `已选 · ${orderMap.get(pid)}`;
      }
    });
    if (selectedCountEl) selectedCountEl.textContent = String(selected.size);
    if (confirmBtnEl) {
      // 0 选中 = 整组淘汰：按钮文案直接切换为警示版（红），单击即执行
      confirmBtnEl.textContent = busy
        ? '处理中…'
        : (selected.size === 0 ? '整组淘汰' : `确认保留 ${selected.size} 张`);
      confirmBtnEl.disabled = busy;
      confirmBtnEl.classList.toggle('btn--danger', !busy && selected.size === 0);
      confirmBtnEl.title = selected.size === 0
        ? '未选中任何照片：点击将把这组全部移入淘汰目录'
        : '快捷键 Enter';
    }
  }

  /* ---------- 决策操作 ---------- */

  function setActionbarBusy(flag) {
    if (confirmBtnEl) confirmBtnEl.disabled = flag;
    if (dismissBtnEl) dismissBtnEl.disabled = flag;
    if (undoBtnEl) undoBtnEl.disabled = flag;
    if (confirmBtnEl) {
      confirmBtnEl.textContent = flag
        ? '处理中…'
        : (selected.size === 0 ? '整组淘汰' : `确认保留 ${selected.size} 张`);
      confirmBtnEl.classList.toggle('btn--danger', !flag && selected.size === 0);
    }
  }

  async function confirmDecision() {
    if (!group || !photos || busy) return;
    if (selected.size === 0) {
      // 保留 0 张 = 整组全部淘汰：按钮文案已警示（红色「整组淘汰」），
      // 无需二次确认；淘汰目录+历史页可撤销是兜底安全网
      const n = photos.length;
      if (n === 0) {
        toast('当前组没有照片', { kind: 'warn' });
        return;
      }
    }
    const keepIds = photos.filter((p) => selected.has(p.photo_id)).map((p) => p.photo_id);
    const learn = learnToggleEl ? learnToggleEl.checked : true;
    busy = true;
    setActionbarBusy(true);
    try {
      const res = await decideGroup(group.group_id, keepIds, learn);
      if (disposed) return;
      lastDecisionGroupId = group.group_id;
      await ensurePendingList(); // 兜底：确保 pending 缓存已就绪（决策前已在 init 预取）
      const nextId = neighborPending(group.group_id, +1); // 先算下一组再出列
      removePendingId(group.group_id);
      const moved = res && res.moved_files != null ? res.moved_files : 0;
      const modelInfo =
        res && res.model ? `偏好模型 v${res.model.version}（样本 ${res.model.sample_count}）` : '';
      toast(
        (keepIds.length === 0
          ? `整组淘汰：${moved} 个文件已全部移入淘汰目录（历史页可撤销）`
          : `决策完成：保留 ${keepIds.length} 张，${moved} 个文件已移入淘汰目录`) +
          (modelInfo ? `\n${modelInfo}` : ''),
        { kind: 'ok', duration: 4500 },
      );
      if (nextId != null) {
        location.hash = `#/compare?group=${nextId}`;
      } else {
        toast('没有更多待决策组，返回总览', { kind: 'info', duration: 4500 });
        location.hash = '#/overview';
      }
    } catch (err) {
      if (disposed) return;
      toast(`决策失败：${err.message}`, { kind: 'error' });
      busy = false;
      setActionbarBusy(false);
    }
  }

  async function dismissCurrent() {
    if (!group || !photos || busy) return;
    busy = true;
    setActionbarBusy(true);
    try {
      await dismissGroup(group.group_id);
      if (disposed) return;
      await ensurePendingList(); // 兜底：确保 pending 缓存已就绪
      const nextId = neighborPending(group.group_id, +1);
      removePendingId(group.group_id);
      toast(`组 #${group.group_id} 已标记为非重复，保留全部 ${photos.length} 张照片`, {
        kind: 'ok',
      });
      if (nextId != null) {
        location.hash = `#/compare?group=${nextId}`;
      } else {
        toast('没有更多待决策组，返回总览', { kind: 'info', duration: 4500 });
        location.hash = '#/overview';
      }
    } catch (err) {
      if (disposed) return;
      toast(`操作失败：${err.message}`, { kind: 'error' });
      busy = false;
      setActionbarBusy(false);
    }
  }

  function currentUndoTarget() {
    if (group && group.status === 'decided') return group.group_id; // 当前组优先
    if (lastDecisionGroupId != null) return lastDecisionGroupId;
    return historyInitGroupId || null;
  }

  async function undoLast() {
    if (busy) return;
    const target = currentUndoTarget();
    if (target == null) {
      toast('当前没有可撤销的决策', { kind: 'info' });
      return;
    }
    busy = true;
    setActionbarBusy(true);
    try {
      const res = await undoGroup(target);
      if (disposed) return;
      addPendingId(target); // 组回到 pending，←/→ 导航重新可见
      if (lastDecisionGroupId === target) lastDecisionGroupId = null;
      if (historyInitGroupId === target) historyInitGroupId = null;
      const restored = res && res.restored_files != null ? res.restored_files : 0;
      toast(`已撤销组 #${target} 的决策，${restored} 个文件已恢复`, { kind: 'ok' });
      if (group && group.group_id === target) {
        await reloadCurrent();
      }
    } catch (err) {
      if (!disposed) toast(`撤销失败：${err.message}`, { kind: 'error' });
    } finally {
      if (!disposed) {
        busy = false;
        setActionbarBusy(false);
      }
    }
  }

  async function reloadCurrent() {
    if (!group) return;
    try {
      const detail = await getGroup(group.group_id);
      if (disposed) return;
      group = detail;
      photos = detail.photos || [];
      buildGroup();
    } catch (err) {
      if (!disposed) toast(`重载组失败：${err.message}`, { kind: 'error' });
    }
  }

  async function gotoPending(dir) {
    if (busy) return;
    await ensurePendingList();
    if (disposed) return;
    const currentId = group ? group.group_id : null;
    let next = null;
    if (currentId === null) {
      next = dir > 0 ? (pendingIdsCache[0] ?? null) : null;
    } else {
      const idx = pendingIdsCache.indexOf(currentId);
      next =
        idx === -1
          ? dir > 0
            ? (pendingIdsCache[0] ?? null)
            : null
          : (pendingIdsCache[idx + dir] ?? null);
    }
    if (next == null) {
      toast(dir > 0 ? '没有下一个待决策组了' : '已经是第一个待决策组', { kind: 'info' });
      return;
    }
    location.hash = `#/compare?group=${next}`;
  }

  /* ---------- 反馈 ---------- */

  async function sendFeedbackFor(photoId, action, btn) {
    if (btn) btn.disabled = true;
    try {
      await sendFeedback(photoId, action);
      if (disposed) return;
      feedbackState.set(photoId, action);
      syncFeedbackUI(photoId);
      toast(`已记录反馈：照片 ${photoId} ${action === 'like' ? '👍 好评' : '👎 差评'}`, {
        kind: 'ok',
        duration: 2400,
      });
    } catch (err) {
      if (!disposed) toast(`反馈失败：${err.message}`, { kind: 'error' });
    } finally {
      if (btn && !disposed) btn.disabled = false;
    }
  }

  function syncFeedbackUI(photoId) {
    const card = section.querySelector(`.photo-card[data-photo-id="${photoId}"]`);
    if (!card) return;
    const state = feedbackState.get(photoId);
    const like = card.querySelector('[data-testid="feedback-like"]');
    const dislike = card.querySelector('[data-testid="feedback-dislike"]');
    if (like) like.classList.toggle('is-active', state === 'like');
    if (dislike) dislike.classList.toggle('is-active', state === 'dislike');
  }

  /* ---------- cheatsheet ---------- */

  function setCheatsheet(open) {
    cheatsheetOpen = open;
    if (cheatsheetEl) cheatsheetEl.hidden = !open;
  }

  /* ---------- 渲染 ---------- */

  function buildLoading() {
    section.innerHTML = '<div class="loading-card" data-testid="loading">加载组数据中…</div>';
  }

  function buildError(err) {
    const status = err && err.status;
    if (status === 404) {
      // 组已不存在：流水线重建会解散无决策历史的 pending 组，旧链接失效。
      // 清掉 id 走默认路径（自动跳第一个待决策组），保持决策流不中断。
      toast('该组已不存在（可能被流水线重建），已跳到下一个待决策组', { kind: 'warn' });
      pendingIdsCache = null; // 组列表已过期，强制重拉
      location.replace('#/compare');
      return;
    }
    section.innerHTML = `
      <div class="empty-state" data-testid="load-error">
        <div class="empty-state__icon" aria-hidden="true">⚠️</div>
        <h2 class="empty-state__title">组数据加载失败</h2>
        <p class="empty-state__text"></p>
        <div class="empty-state__actions">
          <button type="button" class="btn btn--primary" data-testid="retry">重试</button>
          <a class="btn" href="#/overview">返回总览</a>
        </div>
      </div>`;
    section.querySelector('.empty-state__text').textContent = String(err && err.message ? err.message : err);
    section.querySelector('[data-testid="retry"]').addEventListener('click', () => {
      buildLoading();
      init().catch((e2) => {
        if (!disposed) buildError(e2);
      });
    });
  }

  function buildNoTarget() {
    section.innerHTML = `
      <div class="empty-state" data-testid="no-target">
        <div class="empty-state__icon" aria-hidden="true">🎉</div>
        <h2 class="empty-state__title">没有待决策的重复组</h2>
        <p class="empty-state__text">全部处理完毕，或还没有扫描过照片库。</p>
        <div class="empty-state__actions">
          <a class="btn btn--primary" href="#/overview">返回总览</a>
          <a class="btn" href="#/settings">前往设置扫描</a>
        </div>
      </div>`;
  }

  function buildGroup() {
    if (viewer) {
      viewer.destroy();
      viewer = null;
    }
    section.replaceChildren();

    const recId = group.recommended_photo_id;
    // decided groups: preselect what the decision actually kept (falls
    // back to the recommended photo for legacy rows without a decision)
    const decidedKept = Array.isArray(group.kept_photo_ids)
      ? group.kept_photo_ids : null;
    if (decidedKept) {
      selected = new Set(decidedKept);
    } else {
      selected = new Set(recId != null ? [recId] : []);
    }

    // ---- 顶部 ----
    const top = document.createElement('header');
    top.className = 'compare-top';
    const back = document.createElement('a');
    back.className = 'compare-top__back';
    back.href = '#/overview';
    back.dataset.testid = 'back-overview';
    back.textContent = '← 返回总览';

    const title = document.createElement('h1');
    title.className = 'compare-top__title';
    title.dataset.testid = 'compare-title';
    title.textContent = `组 #${group.group_id}`;

    const statusBadge = document.createElement('span');
    statusBadge.className = `badge badge--status badge--status-${group.status}`;
    statusBadge.dataset.testid = 'status-badge';
    statusBadge.textContent = STATUS_LABEL[group.status] || group.status;

    const count = document.createElement('span');
    count.className = 'compare-top__count';
    count.textContent = `${photos.length} 张照片`;

    const toolbar = document.createElement('div');
    toolbar.className = 'viewer-toolbar';
    zoomReadoutEl = document.createElement('span');
    zoomReadoutEl.className = 'zoom-readout';
    zoomReadoutEl.dataset.testid = 'zoom-readout';
    zoomReadoutEl.textContent = '100%';
    zoomReadoutEl.title = '当前同步缩放倍率（滚轮缩放，双击复位）';
    resetBtnEl = document.createElement('button');
    resetBtnEl.type = 'button';
    resetBtnEl.className = 'btn btn--sm';
    resetBtnEl.dataset.testid = 'viewer-reset';
    resetBtnEl.textContent = '复位视图';
    resetBtnEl.disabled = true;
    resetBtnEl.addEventListener('click', () => viewer && viewer.reset(true));
    const helpBtn = document.createElement('button');
    helpBtn.type = 'button';
    helpBtn.className = 'btn btn--sm';
    helpBtn.dataset.testid = 'help-toggle';
    helpBtn.title = '快捷键帮助（按 ? 键）';
    helpBtn.textContent = '? 快捷键';
    helpBtn.addEventListener('click', () => setCheatsheet(!cheatsheetOpen));
    toolbar.append(zoomReadoutEl, resetBtnEl, helpBtn);
    top.append(back, title, statusBadge, count, toolbar);
    section.appendChild(top);

    // ---- 推荐理由横幅（组级一次） ----
    if (group.reason) {
      const banner = document.createElement('div');
      banner.className = 'reason-banner';
      banner.dataset.testid = 'reason-banner';
      banner.innerHTML = `
        <span class="reason-banner__icon" aria-hidden="true">💡</span>
        <div class="reason-banner__body">
          <span class="reason-banner__label">推荐理由</span>
          <p class="reason-banner__text"></p>
        </div>`;
      banner.querySelector('.reason-banner__text').textContent = group.reason;
      section.appendChild(banner);
    }

    // ---- 照片卡网格 ----
    const grid = document.createElement('div');
    grid.className = 'compare-grid';
    grid.dataset.testid = 'compare-grid';
    photos.forEach((p, i) =>
      grid.appendChild(buildCard(p, i, decidedKept)));
    section.appendChild(grid);

    // ---- 吸底操作条 ----
    const bar = document.createElement('div');
    bar.className = 'actionbar';
    bar.dataset.testid = 'actionbar';

    const selWrap = document.createElement('span');
    selWrap.className = 'actionbar__sel';
    selWrap.innerHTML = '已选 <strong data-testid="selected-count">0</strong> 张';

    const learnLabel = document.createElement('label');
    learnLabel.className = 'actionbar__learn';
    learnLabel.title = '决策将作为偏好学习样本，用于调整后续组的推荐排序';
    learnToggleEl = document.createElement('input');
    learnToggleEl.type = 'checkbox';
    learnToggleEl.checked = true; // 默认开
    learnToggleEl.dataset.testid = 'learn-toggle';
    const learnText = document.createElement('span');
    learnText.textContent = '学习偏好';
    learnLabel.append(learnToggleEl, learnText);

    confirmBtnEl = document.createElement('button');
    confirmBtnEl.type = 'button';
    confirmBtnEl.className = 'btn btn--primary';
    confirmBtnEl.dataset.testid = 'confirm-keep';
    confirmBtnEl.title = '快捷键 Enter';
    confirmBtnEl.addEventListener('click', confirmDecision);

    dismissBtnEl = document.createElement('button');
    dismissBtnEl.type = 'button';
    dismissBtnEl.className = 'btn';
    dismissBtnEl.dataset.testid = 'dismiss-group';
    dismissBtnEl.title = '快捷键 D';
    dismissBtnEl.textContent = '标记为非重复';
    dismissBtnEl.addEventListener('click', dismissCurrent);

    undoBtnEl = document.createElement('button');
    undoBtnEl.type = 'button';
    undoBtnEl.className = 'btn btn--ghost';
    undoBtnEl.dataset.testid = 'undo-decision';
    undoBtnEl.title = '撤销当前组（或本会话上一次）的决策，恢复被移动的文件';
    undoBtnEl.textContent = '撤销上个决策';
    undoBtnEl.addEventListener('click', undoLast);

    bar.append(selWrap, learnLabel, confirmBtnEl, dismissBtnEl, undoBtnEl);
    section.appendChild(bar);
    selectedCountEl = bar.querySelector('[data-testid="selected-count"]');

    // ---- 快捷键帮助弹层 ----
    cheatsheetEl = document.createElement('div');
    cheatsheetEl.className = 'cheatsheet';
    cheatsheetEl.dataset.testid = 'cheatsheet';
    cheatsheetEl.hidden = !cheatsheetOpen;
    cheatsheetEl.innerHTML = `
      <div class="cheatsheet__card" role="dialog" aria-modal="true" aria-label="快捷键帮助">
        <h2 class="cheatsheet__title">快捷键</h2>
        <table class="cheatsheet__table"><tbody>
          <tr><td><kbd>1</kbd> … <kbd>9</kbd></td><td>选中 / 取消第 N 张照片</td></tr>
          <tr><td><kbd>0</kbd></td><td>全部取消选择</td></tr>
          <tr><td><kbd>Enter</kbd></td><td>确认保留选中的照片</td></tr>
          <tr><td><kbd>D</kbd></td><td>标记当前组为非重复</td></tr>
          <tr><td><kbd>←</kbd> / <kbd>→</kbd></td><td>上一个 / 下一个待决策组</td></tr>
          <tr><td><kbd>?</kbd></td><td>打开 / 关闭本帮助</td></tr>
          <tr><td><kbd>Esc</kbd></td><td>关闭弹层</td></tr>
          <tr><td>拖动图片</td><td>所有视图同步平移</td></tr>
          <tr><td>滚轮</td><td>所有视图同步缩放（0.2x–8x，光标锚定）</td></tr>
          <tr><td>双击图片</td><td>复位视图</td></tr>
        </tbody></table>
        <button type="button" class="btn btn--sm" data-action="close">关闭</button>
      </div>`;
    cheatsheetEl.addEventListener('click', (e) => {
      if (e.target === cheatsheetEl || e.target.closest('[data-action="close"]')) {
        setCheatsheet(false);
      }
    });
    section.appendChild(cheatsheetEl);

    // ---- SyncViewer 接线 ----
    viewer = createSyncViewer({
      onFrameClick: (idx) => {
        if (idx >= 0 && idx < photos.length) toggleSelect(photos[idx].photo_id);
      },
      onStateChange: (st) => {
        if (disposed) return;
        if (zoomReadoutEl) zoomReadoutEl.textContent = `${Math.round(st.scale * 100)}%`;
        if (resetBtnEl) {
          const identity = st.scale === 1 && st.tx === 0 && st.ty === 0;
          resetBtnEl.disabled = identity;
        }
      },
    });
    grid.querySelectorAll('.viewer__frame').forEach((f) => viewer.attach(f));

    updateSelectionUI();
    for (const pid of feedbackState.keys()) syncFeedbackUI(pid);
  }

  /* ---------- 照片卡 ---------- */

  function buildCard(p, index, decidedKept) {
    const card = document.createElement('article');
    card.className = 'photo-card' + (p.is_recommended ? ' is-recommended' : '');
    card.dataset.photoId = String(p.photo_id);
    card.setAttribute('data-testid', 'photo-card');

    // 序号 + 徽章行
    const badges = document.createElement('div');
    badges.className = 'photo-card__badges';
    const idxBadge = document.createElement('span');
    idxBadge.className = 'badge badge--idx';
    idxBadge.title = '按数字键可快速选中这张照片';
    idxBadge.textContent = String(index + 1);
    badges.appendChild(idxBadge);
    if (p.is_recommended && (!decidedKept || decidedKept.length === 0)) {
      const recBadge = document.createElement('span');
      recBadge.className = 'badge badge--rec';
      recBadge.setAttribute('data-testid', 'rec-badge');
      recBadge.textContent = '★ 推荐';
      badges.appendChild(recBadge);
    }
    // decided group: show the actual decision outcome on each card
    if (decidedKept && decidedKept.length > 0) {
      const outcome = document.createElement('span');
      if (decidedKept.includes(p.photo_id)) {
        outcome.className = 'badge badge--kept';
        outcome.dataset.testid = 'kept-badge';
        outcome.textContent = '✓ 已保留';
      } else {
        outcome.className = 'badge badge--rejected';
        outcome.dataset.testid = 'rejected-badge';
        outcome.textContent = '已淘汰';
      }
      badges.appendChild(outcome);
    }
    const selBadge = document.createElement('span');
    selBadge.className = 'badge badge--sel';
    selBadge.setAttribute('data-testid', 'sel-badge');
    selBadge.hidden = true;
    badges.appendChild(selBadge);
    card.appendChild(badges);

    // ---- SyncViewer 视图 ----
    const pf = primaryFile(p);
    const viewerWrap = document.createElement('div');
    viewerWrap.className = 'viewer';
    const frame = document.createElement('div');
    frame.className = 'viewer__frame';
    frame.setAttribute('data-testid', 'viewer-frame');
    frame.title = '拖动：同步平移 · 滚轮：同步缩放 · 双击：复位 · 单击：选中/取消';
    const skeleton = document.createElement('div');
    skeleton.className = 'viewer__skeleton';
    const img = document.createElement('img');
    img.setAttribute('data-testid', 'viewer-img');
    img.alt = basename(pf && pf.path) || `照片 ${p.photo_id}`;
    img.loading = 'lazy';
    img.src = previewUrl(pf ? pf.file_id : p.primary_file_id, 1600);
    img.addEventListener('load', () => {
      if (!disposed) viewerWrap.classList.add('is-loaded');
    });
    img.addEventListener('error', () => {
      if (!disposed) viewerWrap.classList.add('is-error');
    });
    frame.append(skeleton, img);
    const baseline = document.createElement('div');
    baseline.className = 'viewer__baseline';
    baseline.title = '对齐基准线：所有视图的同一水平位置';
    viewerWrap.append(frame, baseline);
    card.appendChild(viewerWrap);

    // ---- 信息面板 ----
    const info = document.createElement('div');
    info.className = 'photo-card__info';

    const fileline = document.createElement('div');
    fileline.className = 'photo-card__fileline';
    const name = document.createElement('span');
    name.className = 'photo-card__name';
    name.textContent = basename(pf && pf.path) || `照片 ${p.photo_id}`;
    name.title = (pf && pf.path) || '';
    fileline.appendChild(name);
    for (const f of (p.files || []).slice(0, 3)) {
      const fb = document.createElement('span');
      const raw = isRawFile(f);
      fb.className = 'fmt-badge' + (raw ? ' fmt-badge--raw' : '');
      fb.textContent = raw ? 'RAW' : (f.format || 'FILE');
      fb.title = f.path || '';
      fileline.appendChild(fb);
    }
    info.appendChild(fileline);

    const time = document.createElement('div');
    time.className = 'photo-card__time';
    time.textContent = `拍摄于 ${fmtDateTime(p.captured_at)}`;
    info.appendChild(time);

    const exif = p.exif || {};
    const cam = [exif.make, exif.model].filter(Boolean).join(' ');
    const exifItems = [
      ['相机', cam || null],
      ['焦距', exif.focal_length != null ? `${Number(exif.focal_length)}mm` : null],
      ['光圈', fmtAperture(exif.f_number)],
      ['快门', fmtExposure(exif.exposure_time)],
      ['ISO', exif.iso != null ? String(exif.iso) : null],
    ];
    const dl = document.createElement('dl');
    dl.className = 'exif-bar';
    for (const [k, v] of exifItems) {
      const item = document.createElement('div');
      item.className = 'exif-bar__item' + (v ? '' : ' is-empty');
      const dt = document.createElement('dt');
      dt.textContent = k;
      const dd = document.createElement('dd');
      dd.textContent = v || '—';
      item.append(dt, dd);
      dl.appendChild(item);
    }
    info.appendChild(dl);
    card.appendChild(info);

    // ---- 评分 ----
    const dimMax = {};
    for (const d of DIMS) {
      let max = null;
      for (const q of photos) {
        const v = q.scores ? q.scores[d.key] : null;
        if (v != null && (max === null || v > max)) max = v;
      }
      dimMax[d.key] = max;
    }

    const scoresWrap = document.createElement('div');
    scoresWrap.className = 'photo-card__scores';
    const totalRow = document.createElement('div');
    totalRow.className = 'score-total';
    const totalLabel = document.createElement('span');
    totalLabel.className = 'score-total__label';
    totalLabel.textContent = '综合评分';
    const totalValue = document.createElement('strong');
    totalValue.className = 'score-total__value';
    totalValue.setAttribute('data-testid', 'score-total');
    const tv = p.scores ? p.scores.total : null;
    totalValue.textContent = tv == null ? '—' : String(Math.round(tv * 10) / 10);
    totalRow.append(totalLabel, totalValue);
    scoresWrap.appendChild(totalRow);

    const bars = document.createElement('div');
    bars.className = 'score-bars';
    for (const d of DIMS) {
      const v = p.scores ? p.scores[d.key] : null;
      const row = document.createElement('div');
      const isAdvantage = p.is_recommended && v != null && v === dimMax[d.key] && (photos.length > 1);
      row.className = 'score-row' + (isAdvantage ? ' score-row--best' : '');
      row.dataset.dim = d.key;
      const label = document.createElement('span');
      label.className = 'score-row__label';
      label.textContent = d.label;
      const track = document.createElement('span');
      track.className = 'score-row__track';
      const fill = document.createElement('span');
      fill.className = 'score-row__fill';
      if (v != null) fill.style.width = `${Math.max(0, Math.min(100, v))}%`;
      track.appendChild(fill);
      const value = document.createElement('span');
      value.className = 'score-row__value';
      value.textContent = v == null ? '—' : String(Math.round(v));
      row.append(label, track, value);
      bars.appendChild(row);
    }
    scoresWrap.appendChild(bars);
    card.appendChild(scoresWrap);

    // ---- 反馈 ----
    const actions = document.createElement('div');
    actions.className = 'photo-card__actions';
    const like = document.createElement('button');
    like.type = 'button';
    like.className = 'btn btn--feedback';
    like.setAttribute('data-testid', 'feedback-like');
    like.title = '告诉推荐器：这张更符合我的偏好';
    like.textContent = '👍 好评';
    like.addEventListener('click', () => sendFeedbackFor(p.photo_id, 'like', like));
    const dislike = document.createElement('button');
    dislike.type = 'button';
    dislike.className = 'btn btn--feedback';
    dislike.setAttribute('data-testid', 'feedback-dislike');
    dislike.title = '告诉推荐器：这张不符合我的偏好';
    dislike.textContent = '👎 差评';
    dislike.addEventListener('click', () => sendFeedbackFor(p.photo_id, 'dislike', dislike));
    actions.append(like, dislike);
    card.appendChild(actions);

    return card;
  }

  /* ---------- cleanup ---------- */

  return () => {
    disposed = true;
    window.removeEventListener('keydown', onKeydown);
    if (viewer) viewer.destroy();
  };
}
