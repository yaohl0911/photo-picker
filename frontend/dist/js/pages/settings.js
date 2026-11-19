// 设置页（Task 10 核心实现，Task 13a 增加服务端加载）：
//   1. 图库统计卡片 —— 进入页面即 GET /api/overview 渲染；
//   2. 源目录管理 —— 服务端优先：进页并行 GET /api/settings，
//      source_dirs 非空则覆盖本地列表并重写 localStorage；
//      请求失败时降级为 localStorage 本地副本并提示「离线模式」；
//      输入校验（非空、去重），回车/按钮添加，列表可删除；
//   3. 扫描作业 —— 「保存并扫描」POST /api/scan 提交整个列表
//      （后端 update_settings 已持久化，成功后无需额外处理），
//      拿到 job_id 后每 1s 轮询 GET /api/jobs/{id} 更新进度条，
//      finished/failed 停止轮询，完成后刷新统计卡片；
//   4. 完整流水线 —— POST /api/pipeline/run（同步、可能耗时），
//      按钮加载态 + 完成后展示各阶段报告摘要。
//
// localStorage（photo-picker.source_dirs）始终与列表同步：
// 添加/删除/保存时写入，服务端加载成功时以服务端为准重写，
// 仅作为离线降级的后备副本。

import { getOverview, getSettings, startScan, getJob, listJobs, runPipeline } from '../api.js';

const STORAGE_KEY = 'photo-picker.source_dirs';
const POLL_INTERVAL_MS = 1000;

const KIND_LABELS = {
  scan: '索引扫描',
  hashing: '感知哈希',
  analysis: '质量分析',
  execute: '文件移动',
  pipeline: '完整流水线',
};

// pipeline 作业的 note 字段携带当前阶段名（scan/hash/group）
const STAGE_LABELS = {
  scan: '索引扫描',
  hash: '感知哈希',
  group: '相似分组',
  analysis: '质量分析',
  recommend: '推荐计算',
};

// 流水线的五个固定阶段，按执行顺序（job.note 的推进顺序）
const PIPELINE_STAGES = [
  { key: 'scan', label: '索引扫描' },
  { key: 'hash', label: '感知哈希' },
  { key: 'group', label: '相似分组' },
  { key: 'analysis', label: '质量分析' },
  { key: 'recommend', label: '推荐计算' },
];

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
    jobStartedAt: null, // pipeline 总运行时间基准（started_at 毫秒）
    stageStart: null, // 当前阶段起始毫秒（stage_times[note].started）
    elapsedTimer: null, // 每秒跳动运行时间的 interval
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
      <p class="alert alert--error" id="offline-notice" data-testid="offline-notice" hidden></p>
      <ul class="dir-list" id="dir-list" data-testid="dir-list"></ul>
      <div class="card__foot">
        <button class="btn btn--primary" id="scan-btn" type="button">保存并扫描</button>
        <span class="hint">目录列表从服务端加载（localStorage 作为离线备份）；点击「保存并扫描」时整表提交给后端持久化。</span>
      </div>
    </section>

    <section class="card job-card" id="job-card" data-testid="job-card" hidden>
      <div class="card__head">
        <h2 class="card__title">扫描作业</h2>
        <span class="card__meta" id="job-kind" data-testid="job-kind"></span>
      </div>
      <div class="progress" id="job-progress" role="progressbar" aria-label="扫描进度"
           aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div class="progress__fill" id="job-fill" data-testid="job-progress"></div>
      </div>
      <div class="progress-row">
        <span id="job-phase" data-testid="job-phase">准备中…</span>
        <span class="progress-row__pct" id="job-right" data-testid="job-right">0%</span>
      </div>
      <ol class="stage-list" id="job-stages" data-testid="job-stages" hidden></ol>
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
  const jobProgressWrap = $('#job-progress');
  const jobPhase = $('#job-phase');
  const jobRight = $('#job-right');
  const jobError = $('#job-error');
  const jobStages = $('#job-stages');
  const progressBar = section.querySelector('.progress');
  const pipelineBtn = $('#pipeline-btn');
  const pipelineReport = $('#pipeline-report');
  const pipelineError = $('#pipeline-error');
  const offlineNotice = $('#offline-notice');

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

  function showOfflineNotice(message) {
    offlineNotice.hidden = !message;
    if (message) offlineNotice.textContent = message;
  }

  // 服务端优先加载源目录：GET /api/settings 成功且 source_dirs 非空 →
  // 覆盖本地列表并以服务端为准重写 localStorage；请求失败 →
  // 降级显示 localStorage 本地副本，并提示离线模式。
  async function loadServerSettings() {
    try {
      const settings = await getSettings();
      const serverDirs = (Array.isArray(settings.source_dirs) ? settings.source_dirs : [])
        .filter((d) => typeof d === 'string' && d);
      showOfflineNotice(null);
      if (serverDirs.length > 0) {
        state.dirs = serverDirs;
        saveDirs(serverDirs); // 服务端为准，重写本地副本
        renderDirs();
      }
    } catch (err) {
      showOfflineNotice('离线模式，显示本地保存的目录');
    }
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

  // 秒数 → "mm:ss" / "h:mm:ss"（超一小时）
  function fmtElapsed(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '--:--';
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
    return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  }

  // pipeline 作业：右侧显示总运行时间（running 实时跳动，finished 固定）
  function totalElapsedText(job) {
    const started = Date.parse(job.started_at || '');
    if (!Number.isFinite(started)) return '--:--';
    if (job.phase === 'finished' && job.finished_at) {
      const ended = Date.parse(job.finished_at);
      if (Number.isFinite(ended)) return fmtElapsed((ended - started) / 1000);
    }
    return fmtElapsed((Date.now() - started) / 1000);
  }

  function stopElapsedTimer() {
    if (state.elapsedTimer !== null) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }
  }

  // 每秒刷新：总运行时间 + 当前阶段行耗时（跳动的秒表）
  function ensureElapsedTimer() {
    if (state.elapsedTimer !== null) return;
    state.elapsedTimer = setInterval(() => {
      if (state.jobStartedAt != null) {
        jobRight.textContent = fmtElapsed((Date.now() - state.jobStartedAt) / 1000);
      }
      if (state.stageStart != null) {
        const el = jobStages.querySelector('.stage-row.is-current .stage-row__time');
        if (el) el.textContent = fmtElapsed((Date.now() - state.stageStart) / 1000);
      }
    }, 1000);
  }

  function setJobState(job) {
    jobCard.hidden = false;
    jobKind.textContent = `#${job.id} · ${KIND_LABELS[job.kind] || job.kind || '作业'}`;

    const progress = Math.max(0, Math.min(1, Number(job.progress) || 0));
    const pct = Math.round(progress * 100);
    jobFill.style.width = pct + '%';
    progressBar.setAttribute('aria-valuenow', String(pct));

    jobPhase.textContent =
      job.phase === 'running' && job.note
        ? `${PHASE_LABELS[job.phase] || job.phase} · ${STAGE_LABELS[job.note] || job.note}`
        : PHASE_LABELS[job.phase] || job.phase || '未知状态';

    if (job.kind === 'pipeline') {
      // 五个阶段进度条 + 运行时间；不再显示单条总进度条
      jobProgressWrap.hidden = true;
      state.jobStartedAt = Date.parse(job.started_at || '') || null;
      const stageInfo = job.stage_times && job.note ? job.stage_times[job.note] : null;
      state.stageStart = stageInfo ? (Date.parse(stageInfo.started || '') || null) : null;
      jobRight.textContent = totalElapsedText(job);
      if (job.phase === 'running') ensureElapsedTimer();
      else stopElapsedTimer();
      renderStages(job);
    } else {
      jobProgressWrap.hidden = false;
      jobRight.textContent = pct + '%';
      stopElapsedTimer();
      jobStages.hidden = true;
    }
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

  // 流水线分阶段进度：五项列表，各自显示 完成✓ / 当前百分比 / 等待
  // 流水线分阶段进度：五行「阶段名 | 进度条 | 百分比 | 耗时」
  function renderStages(job) {
    const noteIdx = PIPELINE_STAGES.findIndex((s) => s.key === job.note);
    const finished = job.phase === 'finished';
    const progress = Math.max(0, Math.min(1, Number(job.progress) || 0));
    const times = job.stage_times || {};

    jobStages.replaceChildren();
    PIPELINE_STAGES.forEach((stage, i) => {
      let phaseClass = 'is-pending';
      if (finished) {
        phaseClass = 'is-done';
      } else if (noteIdx < 0) {
        phaseClass = i === 0 ? 'is-current' : 'is-pending';
      } else if (i < noteIdx) {
        phaseClass = 'is-done';
      } else if (i === noteIdx) {
        phaseClass = 'is-current';
      }

      const li = document.createElement('li');
      li.className = `stage-row ${phaseClass}`;
      li.dataset.stage = stage.key;

      const name = document.createElement('span');
      name.className = 'stage-row__name';
      name.textContent = stage.label;
      li.appendChild(name);

      const bar = document.createElement('div');
      bar.className = 'stage-row__bar';
      const fill = document.createElement('div');
      fill.className = 'stage-row__fill';
      fill.style.width =
        phaseClass === 'is-done' ? '100%'
        : phaseClass === 'is-current' ? (progress * 100).toFixed(1) + '%'
        : '0%';
      bar.appendChild(fill);
      li.appendChild(bar);

      const pctSpan = document.createElement('span');
      pctSpan.className = 'stage-row__pct';
      pctSpan.textContent =
        phaseClass === 'is-done' ? '100%'
        : phaseClass === 'is-current' ? Math.round(progress * 100) + '%'
        : '—';
      li.appendChild(pctSpan);

      const timeSpan = document.createElement('span');
      timeSpan.className = 'stage-row__time' + (phaseClass === 'is-current' ? ' is-live' : '');
      const t = times[stage.key];
      if (t && t.started) {
        if (phaseClass === 'is-current') {
          timeSpan.textContent = fmtElapsed((Date.now() - Date.parse(t.started)) / 1000);
        } else {
          const ended = t.finished ? Date.parse(t.finished) : Date.now();
          timeSpan.textContent = fmtElapsed((ended - Date.parse(t.started)) / 1000);
        }
      } else if (phaseClass === 'is-done') {
        timeSpan.textContent = '已完成';
      } else {
        timeSpan.textContent = '待开始';
      }
      li.appendChild(timeSpan);

      jobStages.appendChild(li);
    });
    jobStages.hidden = false;
  }

  async function pollOnce() {
    if (state.pollBusy || state.jobId == null) return;
    state.pollBusy = true;
    try {
      const job = await getJob(state.jobId);
      setJobState(job);
      if (job.phase === 'finished') {
        stopPolling();
        await loadStats(); // 作业完成后刷新统计
        if (job.kind === 'pipeline' && job.result) {
          renderPipelineReport(job.result);
          finishPipelineUi();
        }
      } else if (job.phase === 'failed') {
        stopPolling();
        if (job.kind === 'pipeline') {
          pipelineError.textContent = '流水线执行失败：' + (job.error || '未知错误');
          pipelineError.hidden = false;
          finishPipelineUi();
        }
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

  function startPolling(jobId, kind = 'scan') {
    state.jobId = jobId;
    setJobState({ id: jobId, kind, phase: 'running', progress: 0 });
    pollOnce(); // 立即拉一次，避免 1s 空窗
    state.pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
  }

  // 切页再返回时恢复进行中的作业轮询（后台线程还在跑，丢的只是 UI）
  async function resumeActiveJob() {
    try {
      const { jobs } = await listJobs();
      if (!Array.isArray(jobs) || jobs.length === 0) return;
      const active = jobs.find((j) => j.phase === 'running' || j.phase === 'pending');
      if (active) {
        startPolling(active.id, active.kind);
        return;
      }
      // 最近 10 分钟内结束的作业展示终态（失败原因可见）
      const last = jobs[jobs.length - 1];
      const ts = Date.parse(last.finished_at || '');
      if (Number.isFinite(ts) && ts > Date.now() - 10 * 60_000) {
        setJobState(last);
        // 流水线完成后切页返回：报告仍在 job.result 里，原样重渲染
        if (last.kind === 'pipeline' && last.phase === 'finished' && last.result) {
          renderPipelineReport(last.result);
        } else if (last.kind === 'pipeline' && last.phase === 'failed') {
          pipelineError.textContent = '流水线执行失败：' + (last.error || '未知错误');
          pipelineError.hidden = false;
        }
      }
    } catch (err) {
      /* 静默：恢复失败不阻塞设置页 */
    }
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
      saveDirs(state.dirs); // 保存时同步写 localStorage（后端已持久化设置）
      startPolling(jobId);    } catch (err) {
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

  function finishPipelineUi() {
    pipelineBtn.disabled = false;
    pipelineBtn.classList.remove('is-loading');
    pipelineBtn.textContent = '运行完整流水线';
  }

  pipelineBtn.addEventListener('click', async () => {
    if (pipelineBtn.disabled) return;
    if (state.pollTimer !== null) return; // 已有作业在跑（扫描或流水线共用一条轮询）
    pipelineBtn.disabled = true;
    pipelineBtn.classList.add('is-loading');
    pipelineBtn.textContent = '流水线运行中…';
    pipelineError.hidden = true;
    pipelineReport.hidden = true;
    try {
      const { job_id: jobId } = await runPipeline();
      // 后台作业：进度和最终报告都挂在 job 上，切页回来仍可恢复
      startPolling(jobId, 'pipeline');
    } catch (err) {
      pipelineError.textContent = '提交流水线失败：' + err.message;
      pipelineError.hidden = false;
      finishPipelineUi();
    }
  });

  // --- 初始化 ---------------------------------------------------------------------

  renderDirs();
  loadStats();
  loadServerSettings(); // 与统计加载并行：服务端优先，失败降级 localStorage
  resumeActiveJob(); // 恢复后台仍在跑的作业进度（切页返回场景）

  // 离开页面前清掉轮询/计时定时器
  return function cleanup() {
    stopPolling();
    stopElapsedTimer();
  };
}
