// 偏好调优页（Task 12）。
//
// 路由：#/preferences。
//
// 结构：
//   1. 页头 + v0 空态提示（无学习数据 → 引导去对比页决策）
//   2. 模型信息卡：version / 样本数 / bias / 权重语义解释（explanation）
//   3. 五维权重可视化卡：中文名 + 有效权重水平条（相对最大绝对值归一，
//      正负分色）+ 数值 + 与 default_weights 的 ↑↓Δ 对比；
//      custom_weights 覆盖维度带「自定义」徽章，locked 维度带锁图标
//   4. 微调卡：每维 range 滑块 0–3 step 0.05（初值 = effective 值，
//      locked 维度禁用）+「保存自定义权重 / 清除自定义」（PUT weights）
//   5. 锁定管理卡：每维 checkbox +「保存锁定」（PUT locked）
//   6. 训练卡：「用我的选择重新训练」（POST train → toast + 整页刷新）
//
// 语义备注（backend/app/api.py set_preference_weights）：
//   - PUT weights 的 body 为 {weights: {dim: value}}，partial 合并；
//     空 dict {} = 清除全部自定义覆盖（恢复模型权重）。本页保存时
//     始终提交五个维度的完整快照，与滑块所见一致。

import {
  getPreferences,
  updatePreferenceWeights,
  trainPreferences,
  updateLockedDims,
} from '../api.js';
import { toast, fmtNumber } from '../util.js';

/* ---------- 常量 ---------- */

/** 维度展示顺序：缺失维度忽略，后端新维度追加在后。 */
function orderedDims(prefs) {
  const order = ['sharpness', 'exposure', 'noise', 'composition', 'faces'];
  const known = new Set(order);
  const dims = order.filter((d) => prefs.effective_weights[d] !== undefined);
  for (const d of Object.keys(prefs.effective_weights)) {
    if (!known.has(d)) dims.push(d);
  }
  return dims;
}

/** 权重数值展示：null → '—'；否则两位小数。 */
function fmtWeight(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return Number(v).toFixed(2);
}

/** Δ 与阈值比较：小于 0.005 视为无差异（省略箭头）。 */
const DELTA_EPS = 0.005;

/* ---------- 页面 ---------- */

export function renderPreferences(container) {
  const section = document.createElement('section');
  section.className = 'page page--preferences';
  container.appendChild(section);

  let disposed = false;
  let prefs = null; // GET /api/preferences 结果
  let busy = false; // 任一写操作进行中（防重入）

  buildLoading();

  load().catch((err) => {
    if (!disposed) buildError(err);
  });

  async function load() {
    const data = await getPreferences();
    if (disposed) return;
    prefs = data;
    build();
  }

  async function reload() {
    // 写操作后的整页数据刷新（保留当前滚动位置）。
    const y = window.scrollY;
    try {
      prefs = await getPreferences();
      if (disposed) return;
      build();
      window.scrollTo({ top: y, behavior: 'auto' });
    } catch (err) {
      if (!disposed) toast(`偏好数据刷新失败：${err.message}`, { kind: 'error' });
    }
  }

  /* ---------- 渲染骨架 ---------- */

  function buildLoading() {
    section.innerHTML = '<div class="loading-card" data-testid="loading">加载偏好数据中…</div>';
  }

  function buildError(err) {
    section.innerHTML = `
      <div class="empty-state" data-testid="load-error">
        <div class="empty-state__icon" aria-hidden="true">⚠️</div>
        <h2 class="empty-state__title">偏好数据加载失败</h2>
        <p class="empty-state__text"></p>
        <div class="empty-state__actions">
          <button type="button" class="btn btn--primary" data-testid="retry">重试</button>
          <a class="btn" href="#/settings">前往设置</a>
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
        <h1 class="page__title">偏好</h1>
        <p class="page__subtitle">模型从你的每组决策中学习各质量维度的权重；可微调、锁定或重新训练。</p>
      </div>`;
    section.appendChild(head);

    const dims = orderedDims(prefs);
    const isV0 = prefs.version === 0 && (prefs.sample_count || 0) === 0;

    // ---- v0 空态提示 ----
    if (isV0) {
      const empty = document.createElement('div');
      empty.className = 'empty-state pref-empty';
      empty.dataset.testid = 'v0-empty';
      empty.innerHTML = `
        <div class="empty-state__icon" aria-hidden="true">🌱</div>
        <h2 class="empty-state__title">还没有学习数据，先去决策几组照片吧</h2>
        <p class="empty-state__text">每组决策都会成为训练样本，模型会逐渐学出你的偏好权重。下方为默认权重。</p>
        <div class="empty-state__actions">
          <a class="btn btn--primary" href="#/compare" data-testid="v0-go-compare">去对比决策</a>
        </div>`;
      section.appendChild(empty);
    }

    // ---- 模型信息卡 ----
    section.appendChild(buildModelCard());

    // ---- 权重可视化卡 ----
    section.appendChild(buildWeightsCard(dims));

    // ---- 微调卡 ----
    section.appendChild(buildTuningCard(dims));

    // ---- 锁定管理卡 ----
    section.appendChild(buildLockCard(dims));

    // ---- 训练卡 ----
    section.appendChild(buildTrainCard());
  }

  /* ---------- 卡片：模型信息 ---------- */

  function buildModelCard() {
    const card = document.createElement('section');
    card.className = 'card pref-model';
    card.innerHTML = `
      <div class="card__head">
        <h2 class="card__title">模型</h2>
        <span class="card__meta" data-testid="model-meta"></span>
      </div>
      <div class="pref-model__chips">
        <div class="stat-chip">
          <span class="stat-chip__value" data-testid="model-version">—</span>
          <span class="stat-chip__label">模型版本</span>
        </div>
        <div class="stat-chip">
          <span class="stat-chip__value" data-testid="model-samples">—</span>
          <span class="stat-chip__label">学习样本</span>
        </div>
        <div class="stat-chip">
          <span class="stat-chip__value" data-testid="model-bias">—</span>
          <span class="stat-chip__label">偏置 bias</span>
        </div>
      </div>
      <p class="pref-model__explain" data-testid="pref-explain"></p>`;

    card.querySelector('[data-testid="model-version"]').textContent =
      prefs.version === 0 ? '默认' : `v${fmtNumber(prefs.version)}`;
    card.querySelector('[data-testid="model-samples"]').textContent =
      fmtNumber(prefs.sample_count);
    card.querySelector('[data-testid="model-bias"]').textContent =
      fmtWeight(prefs.bias);
    card.querySelector('[data-testid="model-meta"]').textContent =
      prefs.version === 0
        ? '冷启动：使用摄影常识先验权重'
        : `最近训练：${fmtNumber(prefs.sample_count)} 个样本`;
    card.querySelector('[data-testid="pref-explain"]').textContent =
      prefs.explanation || '';
    return card;
  }

  /* ---------- 卡片：五维权重可视化 ---------- */

  function buildWeightsCard(dims) {
    const card = document.createElement('section');
    card.className = 'card';
    card.innerHTML = `
      <div class="card__head">
        <h2 class="card__title">维度权重</h2>
        <span class="card__meta">实际生效值 = 模型权重 + 自定义覆盖（蓝色「自定义」）</span>
      </div>
      <div class="pref-rows" data-testid="pref-weights"></div>`;

    const rowsEl = card.querySelector('[data-testid="pref-weights"]');
    const labels = prefs.labels || {};

    // 归一基准：全部维度有效权重的最大绝对值（<=0 时兜底 1，条宽至少可见）。
    let maxAbs = 0;
    for (const d of dims) {
      const v = Math.abs(Number(prefs.effective_weights[d] || 0));
      if (Number.isFinite(v) && v > maxAbs) maxAbs = v;
    }
    if (maxAbs <= 0) maxAbs = 1;

    for (const dim of dims) {
      const eff = Number(prefs.effective_weights[dim]);
      const def = Number(prefs.default_weights[dim]);
      const locked = (prefs.locked || []).includes(dim);
      const isCustom = Object.prototype.hasOwnProperty.call(
        prefs.custom_weights || {}, dim);

      const row = document.createElement('div');
      row.className = 'pref-row';
      row.dataset.dim = dim;

      // 标签（锁图标 + 中文名 + 自定义徽章）
      const label = document.createElement('span');
      label.className = 'pref-row__label';
      if (locked) {
        const lock = document.createElement('span');
        lock.className = 'pref-row__lock';
        lock.setAttribute('data-testid', `lock-icon-${dim}`);
        lock.title = '已锁定：训练不会移动该维度权重';
        lock.textContent = '🔒';
        lock.setAttribute('aria-label', '已锁定');
        label.appendChild(lock);
      }
      const labelText = document.createElement('span');
      labelText.textContent = labels[dim] || dim;
      label.appendChild(labelText);
      if (isCustom) {
        const badge = document.createElement('span');
        badge.className = 'badge badge--custom';
        badge.setAttribute('data-testid', `custom-badge-${dim}`);
        badge.textContent = '自定义';
        label.appendChild(badge);
      }

      // 水平条（|w|/maxAbs 归一；正值绿 / 负值红）
      const track = document.createElement('span');
      track.className = 'pref-row__track';
      const fill = document.createElement('span');
      fill.className = 'pref-row__fill' + (eff < 0 ? ' is-neg' : '');
      if (Number.isFinite(eff)) {
        const pct = Math.min(100, (Math.abs(eff) / maxAbs) * 100);
        fill.style.width = `${pct}%`;
      }
      track.appendChild(fill);

      // 数值
      const value = document.createElement('span');
      value.className = 'pref-row__value';
      value.dataset.testid = `weight-value-${dim}`;
      value.textContent = fmtWeight(eff);

      row.append(label, track, value, buildDelta(eff, def, dim));
      rowsEl.appendChild(row);
    }
    return card;
  }

  /** 与 default_weights 的对比箭头：|Δ| < eps 时渲染空占位（保持行网格对齐）。 */
  function buildDelta(eff, def, dim) {
    const delta = document.createElement('span');
    delta.className = 'pref-row__delta';
    delta.dataset.testid = `weight-delta-${dim}`;
    if (!Number.isFinite(eff) || !Number.isFinite(def)) return delta;
    const d = eff - def;
    if (Math.abs(d) < DELTA_EPS) return delta; // 无差异省略
    const up = d > 0;
    delta.classList.add(up ? 'is-up' : 'is-down');
    delta.textContent = `${up ? '↑' : '↓'} ${up ? '+' : '−'}${Math.abs(d).toFixed(2)}`;
    delta.title = `默认 ${fmtWeight(def)} → 当前 ${fmtWeight(eff)}`;
    return delta;
  }

  /* ---------- 卡片：微调滑块 ---------- */

  function buildTuningCard(dims) {
    const card = document.createElement('section');
    card.className = 'card';
    card.innerHTML = `
      <div class="card__head">
        <h2 class="card__title">微调自定义权重</h2>
        <span class="card__meta">覆盖模型权重，立即影响推荐排序（0–3）</span>
      </div>
      <div class="pref-rows pref-rows--tuning" data-testid="pref-tuning"></div>
      <div class="card__foot">
        <span class="hint">保存后写入自定义覆盖；清除则恢复为模型权重。锁定维度不可微调。</span>
        <button type="button" class="btn btn--primary" data-testid="save-weights">保存自定义权重</button>
        <button type="button" class="btn" data-testid="clear-weights">清除自定义</button>
      </div>`;

    const rowsEl = card.querySelector('[data-testid="pref-tuning"]');
    const labels = prefs.labels || {};
    const customCount = Object.keys(prefs.custom_weights || {}).length;

    for (const dim of dims) {
      const eff = Number(prefs.effective_weights[dim]);
      const locked = (prefs.locked || []).includes(dim);
      const clamped = Number.isFinite(eff)
        ? Math.min(3, Math.max(0, eff))
        : Number(prefs.default_weights[dim]) || 1;

      const row = document.createElement('div');
      row.className = 'pref-row pref-row--tune';
      row.dataset.dim = dim;

      const label = document.createElement('span');
      label.className = 'pref-row__label';
      label.textContent = labels[dim] || dim;

      const slider = document.createElement('input');
      slider.type = 'range';
      slider.className = 'pref-slider';
      slider.min = '0';
      slider.max = '3';
      slider.step = '0.05';
      slider.value = String(Math.round(clamped * 100) / 100);
      slider.dataset.testid = `slider-${dim}`;
      slider.setAttribute('aria-label', `${labels[dim] || dim} 自定义权重`);
      if (locked) {
        slider.disabled = true;
        slider.title = '维度已锁定，不可微调';
      }

      const readout = document.createElement('span');
      readout.className = 'pref-row__value';
      readout.dataset.testid = `slider-value-${dim}`;
      readout.textContent = fmtWeight(clamped);
      if (!locked) {
        slider.addEventListener('input', () => {
          readout.textContent = Number(slider.value).toFixed(2);
        });
      }

      row.append(label, slider, readout);
      rowsEl.appendChild(row);
    }

    const saveBtn = card.querySelector('[data-testid="save-weights"]');
    const clearBtn = card.querySelector('[data-testid="clear-weights"]');
    saveBtn.addEventListener('click', saveWeights);
    clearBtn.addEventListener('click', clearWeights);
    clearBtn.disabled = customCount === 0;
    clearBtn.title = customCount === 0 ? '当前没有自定义覆盖' : '清除全部自定义覆盖，恢复模型权重';
    return card;
  }

  /* ---------- 卡片：锁定管理 ---------- */

  function buildLockCard(dims) {
    const card = document.createElement('section');
    card.className = 'card';
    card.innerHTML = `
      <div class="card__head">
        <h2 class="card__title">锁定维度</h2>
        <span class="card__meta">锁定后重新训练不会移动该维度权重</span>
      </div>
      <div class="pref-lock-grid" data-testid="pref-locks"></div>
      <div class="card__foot">
        <span class="hint">勾选要固定的维度；保存后立即生效（写入 locked 列表）。</span>
        <button type="button" class="btn btn--primary" data-testid="save-locks">保存锁定</button>
      </div>`;

    const gridEl = card.querySelector('[data-testid="pref-locks"]');
    const labels = prefs.labels || {};
    const lockedSet = new Set(prefs.locked || []);

    for (const dim of dims) {
      const wrap = document.createElement('label');
      wrap.className = 'pref-lock';
      wrap.dataset.dim = dim;
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.dataset.testid = `lock-check-${dim}`;
      box.checked = lockedSet.has(dim);
      const text = document.createElement('span');
      text.textContent = labels[dim] || dim;
      wrap.append(box, text);
      gridEl.appendChild(wrap);
    }

    card
      .querySelector('[data-testid="save-locks"]')
      .addEventListener('click', saveLocks);
    return card;
  }

  /* ---------- 卡片：重新训练 ---------- */

  function buildTrainCard() {
    const card = document.createElement('section');
    card.className = 'card';
    card.innerHTML = `
      <div class="card__head">
        <h2 class="card__title">重新训练</h2>
      </div>
      <p class="hint">用当前全部决策样本重新拟合偏好模型，并按新权重重算所有待决策组的推荐。手动微调与锁定同样参与计算。</p>
      <div class="card__foot">
        <button type="button" class="btn btn--primary" data-testid="train-btn">用我的选择重新训练</button>
      </div>`;
    card
      .querySelector('[data-testid="train-btn"]')
      .addEventListener('click', trainNow);
    return card;
  }

  /* ---------- 写操作 ---------- */

  function guardBusy() {
    if (busy) {
      toast('有操作正在进行中，请稍候', { kind: 'info' });
      return true;
    }
    busy = true;
    return false;
  }

  function releaseBusy() {
    busy = false;
  }

  /** 保存自定义权重：提交五个维度滑块快照（PUT weights，全量合并语义）。 */
  async function saveWeights() {
    if (guardBusy()) return;
    const btn = section.querySelector('[data-testid="save-weights"]');
    if (btn) btn.classList.add('is-loading');
    try {
      const body = {};
      section
        .querySelectorAll('.pref-row--tune')
        .forEach((row) => {
          const slider = row.querySelector('.pref-slider');
          if (slider && !slider.disabled) body[row.dataset.dim] = Number(slider.value);
        });
      // locked 维度滑块禁用：保留其现有自定义值（不覆盖、不清除）。
      for (const dim of Object.keys(prefs.custom_weights || {})) {
        if (!(dim in body)) body[dim] = Number(prefs.custom_weights[dim]);
      }
      const res = await updatePreferenceWeights(body);
      if (disposed) return;
      prefs = res; // PUT 返回与 GET 相同结构
      build();
      toast(
        `自定义权重已保存（${Object.keys(body).length} 个维度，模型 v${fmtNumber(prefs.version)} 生效）`,
        { kind: 'ok' },
      );
    } catch (err) {
      if (!disposed) toast(`保存失败：${err.message}`, { kind: 'error' });
    } finally {
      if (btn) btn.classList.remove('is-loading');
      if (!disposed) releaseBusy();
    }
  }

  /** 清除全部自定义覆盖：PUT weights {}（api.py：空 dict = 重置为默认）。 */
  async function clearWeights() {
    if (guardBusy()) return;
    try {
      const res = await updatePreferenceWeights({});
      if (disposed) return;
      prefs = res;
      build();
      toast('已清除全部自定义覆盖，恢复模型权重', { kind: 'ok' });
    } catch (err) {
      if (!disposed) toast(`清除失败：${err.message}`, { kind: 'error' });
    } finally {
      if (!disposed) releaseBusy();
    }
  }

  /** 保存锁定维度：PUT locked {dims:[...]} → toast + 重新拉取偏好。 */
  async function saveLocks() {
    if (guardBusy()) return;
    const btn = section.querySelector('[data-testid="save-locks"]');
    if (btn) btn.classList.add('is-loading');
    try {
      const dims = [];
      section
        .querySelectorAll('.pref-lock input[type="checkbox"]')
        .forEach((box) => {
          if (box.checked) dims.push(box.dataset.dim || box.closest('.pref-lock').dataset.dim);
        });
      await updateLockedDims(dims);
      if (disposed) return;
      toast(
        dims.length
          ? `已锁定 ${dims.length} 个维度：${dims.map((d) => (prefs.labels || {})[d] || d).join('、')}`
          : '已取消全部锁定',
        { kind: 'ok' },
      );
      await reload();
    } catch (err) {
      if (!disposed) toast(`锁定保存失败：${err.message}`, { kind: 'error' });
    } finally {
      if (btn) btn.classList.remove('is-loading');
      if (!disposed) releaseBusy();
    }
  }

  /** 重新训练：POST train → toast(version/样本/迭代) → 整页刷新。 */
  async function trainNow() {
    if (guardBusy()) return;
    const btn = section.querySelector('[data-testid="train-btn"]');
    if (btn) {
      btn.classList.add('is-loading');
      btn.disabled = true;
      btn.textContent = '训练中…';
    }
    try {
      const res = await trainPreferences();
      if (disposed) return;
      const model = (res && res.model) || {};
      const train = (res && res.train) || {};
      toast(
        `训练完成：模型 v${fmtNumber(
          model.version != null ? model.version : train.version,
        )}（样本 ${fmtNumber(model.sample_count != null ? model.sample_count : train.sample_count)}` +
          (train.iterations != null ? `，${fmtNumber(train.iterations)} 轮迭代` : '') +
          (res && res.recomputed != null ? `，重算 ${fmtNumber(res.recomputed)} 组推荐` : '') +
          '）',
        { kind: 'ok', duration: 5200 },
      );
      await reload();
    } catch (err) {
      if (!disposed) toast(`训练失败：${err.message}`, { kind: 'error' });
    } finally {
      if (btn && !disposed) {
        btn.classList.remove('is-loading');
        btn.disabled = false;
        btn.textContent = '用我的选择重新训练';
      }
      if (!disposed) releaseBusy();
    }
  }

  /* ---------- cleanup ---------- */

  return () => {
    disposed = true;
  };
}
