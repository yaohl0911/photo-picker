// photo-picker 前端 API 封装。
//
// 约定：生产模式下前端由后端 FastAPI 以 StaticFiles 同源托管
// （backend/app/main.py: app.mount("/", StaticFiles(frontend/dist, html=True))），
// 因此默认用相对路径请求 /api/**。
// 集成测试或离线演示时，可在加载本模块前设置 window.__API_BASE__
// （例如 "http://127.0.0.1:8765"）指向真实后端。
//
// 错误约定：所有非 2xx 响应解析 JSON 体中的 detail 字段（FastAPI 的
// 错误格式 {"detail": "..."}），抛出 Error(message)；网络不可达时同样抛错。

const BASE = () => (typeof window !== 'undefined' && window.__API_BASE__) || '';

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(BASE() + path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new ApiError('无法连接后端服务（' + err.message + '）', 0);
  }
  if (!res.ok) {
    let message = 'HTTP ' + res.status;
    try {
      const data = await res.json();
      if (data && data.detail !== undefined) message = String(data.detail);
    } catch (_) { /* 非 JSON 错误体：保留 HTTP 状态描述 */ }
    throw new ApiError(message, res.status);
  }
  return res.json();
}

// --- 总览 / 分组 ------------------------------------------------------------

/** 图库统计总览。 */
export const getOverview = () => request('/api/overview');

/** 相似组列表（默认 pending，分页）。 */
export const listGroups = ({ status = 'pending', offset = 0, limit = 50 } = {}) =>
  request(`/api/groups?status=${encodeURIComponent(status)}&offset=${offset}&limit=${limit}`);

/** 单个组详情（含 ranked 顺序、每张照片的评分与缩略图 URL）。 */
export const getGroup = (groupId) => request(`/api/groups/${groupId}`);

/** 提交一组的保留决策；learn=false 时不进入偏好学习。 */
export const decideGroup = (groupId, keepPhotoIds, learn = true) =>
  request(`/api/groups/${groupId}/decision`, {
    method: 'POST',
    body: { keep_photo_ids: keepPhotoIds, learn },
  });

/** 标记整组为「非重复」并解散。 */
export const dismissGroup = (groupId) =>
  request(`/api/groups/${groupId}/dismiss`, { method: 'POST' });

/** 撤销该组的淘汰移动（文件移回原位）。 */
export const undoGroup = (groupId) =>
  request(`/api/groups/${groupId}/undo`, { method: 'POST' });

// --- 反馈 / 偏好 ------------------------------------------------------------

/** 显式 👍/👎 反馈（action: 'like' | 'dislike'）。 */
export const sendFeedback = (photoId, action) =>
  request('/api/feedback', { method: 'POST', body: { photo_id: photoId, action } });

/** 偏好模型完整状态（权重 / 样本数 / 锁定维度 / 解释）。 */
export const getPreferences = () => request('/api/preferences');

/** 手动微调权重。 */
export const updatePreferenceWeights = (weights) =>
  request('/api/preferences/weights', { method: 'PUT', body: { weights } });

/** 立即触发一次重训练。 */
export const trainPreferences = () =>
  request('/api/preferences/train', { method: 'POST' });

/** 设置锁定的维度（锁定后不参与训练优化）。 */
export const updateLockedDims = (dims) =>
  request('/api/preferences/locked', { method: 'PUT', body: { dims } });

// --- 历史 -------------------------------------------------------------------

export const listHistory = ({ offset = 0, limit = 50, source = 'all' } = {}) =>
  request(`/api/history?offset=${offset}&limit=${limit}&source=${source}`);

// --- 截图清理 -----------------------------------------------------------------

/** 截图列表（懒分析：首次调用触发全库识别）。 */
export const getScreenshots = ({ offset = 0, limit = 60, when = 'all' } = {}) =>
  request(`/api/screenshots?offset=${offset}&limit=${limit}&when=${when}`);

/** 批量淘汰截图（走正常决策管线，可撤销）。 */
export const rejectScreenshots = (photoIds) =>
  request('/api/screenshots/reject', {
    method: 'POST',
    body: { photo_ids: photoIds },
  });

// --- 设置 ---------------------------------------------------------------------

/** 当前服务端设置（源目录 / 淘汰目录 / 相似判定阈值），设置页服务端优先加载用。 */
export const getSettings = () => request('/api/settings');
export const updateSettings = (payload) => request('/api/settings', {
  method: 'PUT',
  body: payload,
});

/** 目录浏览（源目录选择器）：列某目录下的子目录，~ 展开。 */
export const fsList = (path) =>
  request(`/api/fs/list?path=${encodeURIComponent(path)}`);

// --- 扫描 / 作业 / 流水线 -----------------------------------------------------

/** 提交源目录并启动扫描作业，返回 {job_id}。 */
export const startScan = (sourceDirs) =>
  request('/api/scan', { method: 'POST', body: { source_dirs: sourceDirs } });

/** 全部后台作业列表。 */
export const listJobs = () => request('/api/jobs');

/** 单个作业状态：{id, kind, phase, progress, error, note, started_at, finished_at}。 */
export const getJob = (jobId) => request(`/api/jobs/${jobId}`);

/** 同步执行完整流水线（扫描→哈希→分组→分析→推荐），返回各阶段报告。 */
// 完整流水线已改为后台作业：立即返回 {job_id}，进度与最终五阶段报告
// 都在 GET /api/jobs/{id} 的响应里（完成时 result 字段携带报告）。
export const runPipeline = () =>
  request('/api/pipeline/run', { method: 'POST' });

// --- 图片 URL 构造（供 <img> src 直接使用，不走 fetch） ------------------------

export const thumbUrl = (fileId, size = 256) =>
  `${BASE()}/api/files/${fileId}/thumbnail?size=${size}`;

export const previewUrl = (fileId, maxSide = 1600) =>
  `${BASE()}/api/files/${fileId}/preview?max_side=${maxSide}`;
