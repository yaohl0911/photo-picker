// 设置页（Task 10 核心实现）：
//   1. 图库统计卡片 —— 进入页面即 GET /api/overview 渲染；
//   2. 源目录管理 —— 本地列表（localStorage 持久化，初始为空），
//      输入校验（非空、去重），回车/按钮添加，列表可删除；
//   3. 扫描作业 —— 「保存并扫描」POST /api/scan 提交整个列表，
//      拿到 job_id 后每 1s 轮询 GET /api/jobs/{id} 更新进度条，
//      finished/failed 停止轮询，完成后刷新统计卡片；
//   4. 完整流水线 —— POST /api/pipeline/run（同步、可能耗时），
//      按钮加载态 + 完成后展示各阶段报告摘要。
//
// 注：后端目前没有 GET settings 端点，源目录无法从服务端读回，
// 因此列表维护在浏览器 localStorage；每次「保存并扫描」把整表
// 随 POST /api/scan 的 source_dirs 一次性提交（后端会存入 settings）。
// 后端补上 GET /settings 后（Task 13 候选），可改为服务端为准。

import { getOverview, startScan, getJob, runPipeline } from '../api.js';

const STORAGE_KEY = 'photo-picker.source_dirs';
const POLL_INTERVAL_MS = 1000;

const KIND_LABELS = {
  scan: '索引扫描',
  hashing: '感知哈希',
  analysis: '质量分析',
  execute: '文件移动',
};

const PHASE_LABELS = {
  running: '进行中',
  finished: '已完成',
  failed: '失败',
};

const STATS = [
  { key: 'total_photos', label: '总照片' },
  { key: 'total_files', label: '总文件' },
  { key: 'pending_groups', label: '待决策组' },
  { key: 'decided_groups', label: '已决策组' },
  { key: 'samples', label: '学习样本' },
  { key: 'model_version', label: '模型版本', format: 'version' },
];

// --- 本地持久化 ---------------------------------------------------------------

function loadDirs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter((d) => typeof d === 'string' && d)
      : [];
  } catch (_) {
    return [];
  }
}

function saveDirs(dirs) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(dirs));
  } catch (_) { /* 隐私模式等场景：仅本次会话有效 */ }
}

function fmtNumber(v) {
  return v == null ? '–' : Number(v).toLocaleString('zh-CN');
}

function fmtTime(iso) {
  if (!iso) return '尚未扫描';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString('zh-CN', { hour12: false });
}

// --- 页面 ---------------------------------------------------------------------

export function renderSettings(container) {
  const state = {
    dirs: loadDirs(),
    jobId: null,
    pollTimer: null,
    pollBusy: false,
  };

  const section = document.createElement('section');
  section.className = 'page page--settings';
  section.innerHTML = `
    <header class="page__header">
      <h1 class="page__title">设置与扫描</h1>
      <p class="page__desc">管理照片源目录，扫描建立索引，并在后台完成哈希与质量分析。</p>
    </header>

    <section class="card" aria-labelledby="stats-title">
      <div class="card__head">
        <h2 class="card__title" id="stats-title">图库统计</h2>
        <span class="card__meta" data-testid="last-scan">上次扫描：尚未扫描</span>
      </div>
      <div class="stat-grid">
        ${STATS.map((s) => `
          <div class="stat" data-stat="${s.key}">
            <div class="stat__value" data-testid="stat-${s.key}">–</div>
            <div class="stat__label">${s.label}</div>
          </div>`).join('')}
      </div>
      <p class="alert alert--error" data-testid="stats-error" hidden></p>
    </section>

    <section class="card" aria-labelledby="dirs-title">
      <div class="card__head">
        <h2 class="card__title" id="dirs-title">源目录</h2>
      </div>
      <form class="dir-form" id="dir-form">
        <input class="input dir-form__input" id="dir-input" type="text"
               placeholder="输入照片目录的绝对路径，例如 /Users/you/Pictures/2024"
               autocomplete="off" autocapitalize="off" spellcheck="false"
               aria-label="源目录路径">
        <button class="btn dir-form__add" id="dir-add-btn" type="submit">添加</button>
      </form>
      <p class="field-error" id="dir-error" data-testid="dir-error" hidden></p>
      <ul class="dir-list" id="dir-list" data-testid="dir-list"></ul>
      <div class="card__foot">
        <button class="btn btn--primary" id="scan-btn" type="button">保存并扫描</button>
        <span class="hint">目录列表保存在本浏览器（localStorage）；点击「保存并扫描」时整表提交给后端。</span>
      </div>
    </section>

    <section class="card job-card" id="job-card" data-testid="job-card" hidden>
      <div class="card__head">
        <h2 class="card__title">扫描作业</h2>
        <span class="card__meta" id="job-kind" data-testid="job-kind"></span>
      </div>
      <div class="progress" role="progressbar" aria-label="扫描进度"
           aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div class="progress__fill" id="job-fill" data-testid="job-progress"></div>
      </div>
      <div class="progress-row">
        <span id="job-phase" data-testid="job-phase">准备中…</span>
        <span class="progress-row__pct" id="job-pct" data-testid="job-pct">0%</span>
      </div>
      <p class="alert alert--error" id="job-error" data-testid="job-error" hidden></p>
    </section>

    <section class="card" aria-labelledby="pipeline-title">
      <div class="card__head">
        <h2 class="card__title" id="pipeline-title">完整流水线</h2>
      </div>
      <p class="hint">一次性执行：索引扫描 → 感知哈希 → 相似分组 → 质量分析 → 推荐计算。照片较多时耗时较长。</p>
      <div class="card__foot">
        <button class="btn" id="pipeline-btn" type="button">运行完整流水线</button>
      </div>
      <ul class="report-list" id="pipeline-report" data-testid="pipeline-report" hidden></ul>
      <p class="alert alert--error" id="pipeline-error" data-testid="pipeline-error" hidden></p>
    </section>
  `;
  container.appendChild(section);

  // --- 元素引用 ---------------------------------------------------------------

  const $ = (sel) => section.querySelector(sel);
  const dirForm = $('#dir-form');
  const dirInput = $('#dir-input');
  const dirError = $('#dir-error');
  const dirList = $('#dir-list');
  const scanBtn = $('#scan-btn');
  const jobCard = $('#job-card');
  const jobKind = $('#job-kind');
  const jobFill = $('#job-fill');
  const jobPhase = $('#job-phase');
  const jobPct = $('#job-pct');
  const jobError = $('#job-error');
  const progressBar = section.querySelector('.progress');
  const pipelineBtn = $('#pipeline-btn');
  const pipelineReport = $('#pipeline-report');
  const pipelineError = $('#pipeline-error');

  // --- 统计 -------------------------------------------------------------------

  function setStatsError(message) {
    statsError.hidden = !message;
    if (message) statsError.textContent = message;
  }
  const statsError = $('[data-testid="stats-error"]');

  async function loadStats() {
    try {
      const data = await getOverview();
      for (const s of STATS) {
        const el = $(`[data-testid="stat-${s.key}"]`);
        const value = data[s.key];
        if (s.format === 'version') {
          el.textContent = value == null ? '–' : 'v' + value;
        } else {
          el.textContent = fmtNumber(value);
        }
      }
      $('[data-testid="last-scan"]').textContent = '上次扫描：' + fmtTime(data.last_scan_at);
      setStatsError(null);
    } catch (err) {
      setStatsError('统计加载失败：' + err.message);
    }
  }

  // --- 源目录列表 ---------------------------------------------------------------

  function showDirError(message) {
    dirError.hidden = !message;
    if (message) dirError.textContent = message;
  }

  function renderDirs() {
    dirList.replaceChildren();
    if (state.dirs.length === 0) {
      const empty = document.createElement('li');
      empty.className = 'dir-list__empty';
      empty.textContent = '尚未添加源目录——添加后即可开始扫描。';
      dirList.appendChild(empty);
      return;
    }
    for (const dir of state.dirs) {
      const li = document.createElement('li');
      li.className = 'dir-item';
      li.dataset.testid = 'dir-item';

      const path = document.createElement('code');
      path.className = 'dir-item__path';
      path.textContent = dir;

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'dir-item__remove';
      remove.dataset.testid = 'dir-remove';
      remove.textContent = '×';
      remove.title = '移除该目录';
      remove.setAttribute('aria-label', '移除目录 ' + dir);
      remove.addEventListener('click', () => {
        state.dirs = state.dirs.filter((d) => d !== dir);
        saveDirs(state.dirs);
        renderDirs();
      });

      li.append(path, remove);
      dirList.appendChild(li);
    }
  }

  dirForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = dirInput.value.trim();
    if (!value) {
      showDirError('请输入目录路径');
      dirInput.focus();
      return;
    }
    if (state.dirs.includes(value)) {
      showDirError('该目录已在列表中');
      dirInput.focus();
      return;
    }
    showDirError(null);
    state.dirs.push(value);
    saveDirs(state.dirs);
    dirInput.value = '';
    renderDirs();
    dirInput.focus();
  });

  dirInput.addEventListener('input', () => showDirError(null));

  // --- 扫描作业轮询 ---------------------------------------------------------------

  function setJobState(job) {
    jobCard.hidden = false;
    jobKind.textContent = `#${job.id} · ${KIND_LABELS[job.kind] || job.kind || '作业'}`;

    const progress = Math.max(0, Math.min(1, Number(job.progress) || 0));
    const pct = Math.round(progress * 100);
    jobFill.style.width = pct + '%';
    jobPct.textContent = pct + '%';
    progressBar.setAttribute('aria-valuenow', String(pct));

    jobPhase.textContent = PHASE_LABELS[job.phase] || job.phase || '未知状态';
    jobFill.classList.toggle('is-done', job.phase === 'finished');
    jobFill.classList.toggle('is-failed', job.phase === 'failed');

    if (job.phase === 'failed') {
      jobError.textContent = job.error || '作业失败（后端未返回错误信息）';
      jobError.hidden = false;
    } else {
      jobError.hidden = true;
    }
  }

  function stopPolling() {
    if (state.pollTimer !== null) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function pollOnce() {
    if (state.pollBusy || state.jobId == null) return;
    state.pollBusy = true;
    try {
      const job = await getJob(state.jobId);
      setJobState(job);
      if (job.phase === 'finished') {
        stopPolling();
        await loadStats(); // 扫描完成后刷新统计
      } else if (job.phase === 'failed') {
        stopPolling();
      }
    } catch (err) {
      stopPolling();
      setJobState({
        id: state.jobId,
        kind: 'scan',
        phase: 'failed',
        progress: 1,
        error: '轮询作业状态失败：' + err.message,
      });
    } finally {
      state.pollBusy = false;
    }
  }

  function startPolling(jobId) {
    state.jobId = jobId;
    setJobState({ id: jobId, kind: 'scan', phase: 'running', progress: 0 });
    pollOnce(); // 立即拉一次，避免 1s 空窗
    state.pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
  }

  scanBtn.addEventListener('click', async () => {
    if (state.pollTimer !== null) return; // 已有作业在跑
    if (state.dirs.length === 0) {
      showDirError('请先添加至少一个源目录');
      return;
    }
    showDirError(null);
    scanBtn.disabled = true;
    scanBtn.classList.add('is-loading');
    scanBtn.textContent = '提交扫描…';
    try {
      const { job_id: jobId } = await startScan(state.dirs);
      startPolling(jobId);
    } catch (err) {
      setJobState({
        id: '–',
        kind: 'scan',
        phase: 'failed',
        progress: 1,
        error: '提交扫描失败：' + err.message,
      });
    } finally {
      scanBtn.disabled = false;
      scanBtn.classList.remove('is-loading');
      scanBtn.textContent = '保存并扫描';
    }
  });

  // --- 完整流水线 -----------------------------------------------------------------

  function renderPipelineReport(report) {
    const n = (v) => (v == null ? 0 : Number(v).toLocaleString('zh-CN'));
    const lines = [];
    const scan = report.scan || {};
    lines.push(`索引扫描：新增 ${n(scan.new)} / 变更 ${n(scan.changed)} / 共 ${n(scan.total)} 个文件，RAW+JPG 配对 ${n(scan.pairs)} 组`);
    const hashing = report.hashing || {};
    lines.push(`感知哈希：计算 ${n(hashing.hashed)} 个，跳过 ${n(hashing.skipped)} 个`);
    const grouping = report.grouping || {};
    lines.push(`相似分组：${n(grouping.groups)} 组（验证候选对 ${n(grouping.pairs)} 对）`);
    const analysis = report.analysis || {};
    lines.push(`质量分析：完成 ${n(analysis.analyzed)} 张，失败 ${n(analysis.failed)} 张`);
    const recommend = report.recommend || {};
    lines.push(`推荐计算：更新 ${n(recommend.recalculated)} 组`);

    const errorCount =
      (Array.isArray(scan.errors) ? scan.errors.length : 0) +
      (Array.isArray(hashing.errors) ? hashing.errors.length : 0) +
      (Array.isArray(analysis.errors) ? analysis.errors.length : 0);
    if (errorCount > 0) {
      lines.push(`⚠ 共 ${n(errorCount)} 条错误（部分文件无法解码或分析，已跳过）`);
    }

    pipelineReport.replaceChildren();
    for (const line of lines) {
      const li = document.createElement('li');
      if (line.startsWith('⚠')) li.classList.add('is-error');
      li.textContent = line;
      pipelineReport.appendChild(li);
    }
    pipelineReport.hidden = false;
  }

  pipelineBtn.addEventListener('click', async () => {
    if (pipelineBtn.disabled) return;
    pipelineBtn.disabled = true;
    pipelineBtn.classList.add('is-loading');
    pipelineBtn.textContent = '流水线运行中…';
    pipelineError.hidden = true;
    pipelineReport.hidden = true;
    try {
      const report = await runPipeline();
      renderPipelineReport(report);
      await loadStats(); // 分析完成后刷新统计
    } catch (err) {
      pipelineError.textContent = '流水线执行失败：' + err.message;
      pipelineError.hidden = false;
    } finally {
      pipelineBtn.disabled = false;
      pipelineBtn.classList.remove('is-loading');
      pipelineBtn.textContent = '运行完整流水线';
    }
  });

  // --- 初始化 ---------------------------------------------------------------------

  renderDirs();
  loadStats();

  // 离开页面前清掉轮询定时器
  return function cleanup() {
    stopPolling();
  };
}
