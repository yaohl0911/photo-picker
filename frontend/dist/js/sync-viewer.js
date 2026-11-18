/**
 * sync-viewer.js — Task 11 对比页核心组件：同步缩放查看器（SyncViewer）。
 *
 * 一组等尺寸的 .viewer__frame（内部各含一张 <img>，图片以等高居中方式填充）
 * 共享同一个变换状态 {scale, tx, ty}：
 *   - 拖动任意一张 → 所有图同步平移（Pointer Events + setPointerCapture）
 *   - 滚轮 → 所有图同步缩放（0.2x–8x，以光标位置为锚点）
 *   - 双击 → 复位（带 180ms 过渡动画）
 *
 * 变换方案：每张 img 应用 `translate(tx,ty) scale(s)`，transform-origin 为图片中心
 * （图片在 frame 内 flex 居中，故图片中心 ≈ frame 中心）。
 * 滚轮锚定缩放推导：设光标到 frame 中心的向量为 c，缩放比 k = s'/s，
 * 要使光标下的图像点保持不动：t' = c - k·(c - t)。
 * 由于所有 frame 等尺寸且图片居中，同一 c（相对各自 frame 中心）对所有图成立，
 * 因此全组同步锚定缩放。
 */

const MIN_SCALE = 0.2;
const MAX_SCALE = 8;
const CLICK_SLOP_PX = 4; // 拖动距离小于此值视为点击
const ANIM_MS = 180;

export function createSyncViewer({ onFrameClick, onStateChange } = {}) {
  const frames = []; // { el, img }
  const state = { scale: 1, tx: 0, ty: 0 };

  let animTimer = null;

  /* ---------- 变换应用 ---------- */

  function transformString() {
    const { scale, tx, ty } = state;
    return `translate(${tx.toFixed(2)}px, ${ty.toFixed(2)}px) scale(${scale.toFixed(4)})`;
  }

  function applyTransform() {
    const str = transformString();
    for (const { img } of frames) {
      img.style.transform = str;
    }
    if (typeof onStateChange === 'function') {
      onStateChange({ ...state, frames: frames.length });
    }
  }

  /* ---------- 缩放（锚定光标） ---------- */

  function zoomAt(cx, cy, nextScale) {
    const clamped = Math.min(MAX_SCALE, Math.max(MIN_SCALE, nextScale));
    const k = clamped / state.scale;
    if (k === 1) return;
    // t' = c - k·(c - t)
    state.tx = cx - k * (cx - state.tx);
    state.ty = cy - k * (cy - state.ty);
    state.scale = clamped;
    applyTransform();
  }

  function onWheel(e) {
    e.preventDefault();
    const frame = e.currentTarget;
    const rect = frame.getBoundingClientRect();
    const cx = e.clientX - rect.left - rect.width / 2;
    const cy = e.clientY - rect.top - rect.height / 2;
    let dy = e.deltaY;
    if (e.deltaMode === 1) dy *= 16; // 行模式（Firefox）
    else if (e.deltaMode === 2) dy *= 100; // 页模式
    const factor = Math.exp(-dy * 0.0018);
    zoomAt(cx, cy, state.scale * factor);
  }

  /* ---------- 拖拽平移（Pointer Events，兼容触摸） ---------- */

  const drag = { active: false, frame: null, startX: 0, startY: 0, origTx: 0, origTy: 0, moved: false };

  function onPointerDown(e) {
    if (drag.active) return;
    if (e.button !== undefined && e.button !== 0) return; // 仅主键/触摸/笔
    const frame = e.currentTarget;
    drag.active = true;
    drag.frame = frame;
    drag.startX = e.clientX;
    drag.startY = e.clientY;
    drag.origTx = state.tx;
    drag.origTy = state.ty;
    drag.moved = false;
    try {
      frame.setPointerCapture(e.pointerId);
    } catch (_) { /* 老浏览器忽略 */ }
    frame.classList.add('is-dragging');
    e.preventDefault(); // 防止图片原生拖拽/文本选择
  }

  function onPointerMove(e) {
    if (!drag.active || drag.frame !== e.currentTarget) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    if (!drag.moved && Math.hypot(dx, dy) > CLICK_SLOP_PX) drag.moved = true;
    if (!drag.moved) return;
    state.tx = drag.origTx + dx;
    state.ty = drag.origTy + dy;
    applyTransform();
  }

  function endDrag(e) {
    if (!drag.active || drag.frame !== e.currentTarget) return;
    const frame = drag.frame;
    const wasClick = !drag.moved;
    const idx = frames.findIndex((f) => f.el === frame);
    try {
      frame.releasePointerCapture(e.pointerId);
    } catch (_) { /* 已释放 */ }
    frame.classList.remove('is-dragging');
    drag.active = false;
    drag.frame = null;
    if (wasClick && typeof onFrameClick === 'function') onFrameClick(idx, e);
  }

  function onPointerCancel(e) {
    if (!drag.active || drag.frame !== e.currentTarget) return;
    const frame = drag.frame;
    try {
      frame.releasePointerCapture(e.pointerId);
    } catch (_) { /* 已释放 */ }
    frame.classList.remove('is-dragging');
    drag.active = false;
    drag.frame = null;
  }

  /* ---------- 双击复位 ---------- */

  function reset(animate = true) {
    state.scale = 1;
    state.tx = 0;
    state.ty = 0;
    if (animTimer) clearTimeout(animTimer);
    if (animate && frames.length) {
      for (const { img } of frames) img.classList.add('is-animating');
      applyTransform();
      animTimer = setTimeout(() => {
        animTimer = null;
        for (const { img } of frames) img.classList.remove('is-animating');
      }, ANIM_MS + 30);
    } else {
      applyTransform();
    }
  }

  function onDblClick(e) {
    e.preventDefault();
    reset(true);
  }

  /* ---------- 注册 / 销毁 ---------- */

  /**
   * 注册一个查看帧。frameEl 需为 .viewer__frame 且内部含 <img>。
   * @returns {void}
   */
  function attach(frameEl) {
    const img = frameEl.querySelector('img');
    if (!img) throw new Error('sync-viewer: frame 内未找到 <img>');
    img.draggable = false;
    // 初始变换（保持与 state 一致，避免懒加载图首次应用时闪跳）
    img.style.transform = transformString();

    frameEl.addEventListener('pointerdown', onPointerDown);
    frameEl.addEventListener('pointermove', onPointerMove);
    frameEl.addEventListener('pointerup', endDrag);
    frameEl.addEventListener('pointercancel', onPointerCancel);
    frameEl.addEventListener('wheel', onWheel, { passive: false });
    frameEl.addEventListener('dblclick', onDblClick);

    frames.push({ el: frameEl, img });
  }

  function destroy() {
    if (animTimer) clearTimeout(animTimer);
    for (const { el } of frames) {
      el.removeEventListener('pointerdown', onPointerDown);
      el.removeEventListener('pointermove', onPointerMove);
      el.removeEventListener('pointerup', endDrag);
      el.removeEventListener('pointercancel', onPointerCancel);
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('dblclick', onDblClick);
      el.classList.remove('is-dragging');
      for (const img of el.querySelectorAll('img')) img.classList.remove('is-animating');
    }
    frames.length = 0;
    drag.active = false;
    drag.frame = null;
  }

  return {
    attach,
    reset,
    destroy,
    getState: () => ({ ...state, frames: frames.length }),
    /** 供测试/外部重置到指定状态（不触发动画） */
    setState(next) {
      if (typeof next.scale === 'number') state.scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next.scale));
      if (typeof next.tx === 'number') state.tx = next.tx;
      if (typeof next.ty === 'number') state.ty = next.ty;
      applyTransform();
    },
  };
}
