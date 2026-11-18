# 照片去重与偏好学习工具（photo-picker）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本地 Web 应用：扫描文件夹识别连拍/同场景近似重复照片，每组推荐最佳一张并给出理由，多轮选择后学习用户偏好；淘汰照片安全移入专门文件夹可撤销。

**Architecture:** FastAPI 后端（scanner→grouper→quality→recommender→learner→executor 流水线，SQLite 持久化，后台 jobs 队列）+ React/Vite/TS 前端（设置/总览/对比/偏好四页，同步缩放对比为核心交互）。

**Tech Stack:** Python 3.12（现有 .venv：fastapi/uvicorn/imagehash/opencv/pillow-heif/rawpy/scipy/pytest/httpx，零新增依赖；可选 mediapipe）；前端 React 18 + Vite 5 + TypeScript。

**设计 spec：** `docs/superpowers/specs/2026-11-18-photo-dedup-design.md`（需求确认表、数据模型、模块边界——执行前必读）

**约定：** 后端代码在 `backend/`，测试 `backend/tests/`，pytest 从 `backend/` 目录用 `.venv/bin/python -m pytest` 运行；每 Task 完成即 commit。

---

### Task 1: 项目骨架 + db.py + config.py

**Files:**
- Create: `backend/app/__init__.py`、`backend/app/db.py`、`backend/app/config.py`、`backend/tests/conftest.py`、`backend/tests/test_db.py`、`backend/pyproject.toml`

- [ ] **Step 1: 写失败测试** `backend/tests/test_db.py`：init_db 建全部 9 张表（files/photos/groups/quality_scores/decisions/feedback_events/preference_model/move_log/scan_runs）；photos.group_id 外键可空；files.path 唯一冲突报 IntegrityError。

- [ ] **Step 2: 运行验证失败**：`.venv/bin/python -m pytest backend/tests/test_db.py -v`（从仓库根；conftest 把 `backend/` 加入 sys.path）

- [ ] **Step 3: 实现** `backend/app/db.py`：
  - `connect(db_path) -> sqlite3.Connection`（row_factory=Row，WAL，foreign_keys ON）
  - `SCHEMA_VERSION = 1`、`init_db(conn)` 执行建表 DDL（见 spec §4 字段），meta 表存版本
  - `get_db()` app 级单例连接（threading.Lock 保护，check_same_thread=False）
  - `backend/app/config.py`：`get_settings(conn) -> dict`（source_dirs:[]、phash_threshold:10、time_window_sec:60、rejected_root:''、face_engine:auto）与 `update_settings(conn, **kw)`，存 settings KV 表
  - `backend/pyproject.toml`：pytest 配置 `testpaths=["tests"]`

- [ ] **Step 4: 测试通过 + commit** `feat: db schema and settings`

### Task 2: scanner.py（发现/EXIF/增量/RAW-JPG 配对）

**Files:**
- Create: `backend/app/scanner.py`、`backend/tests/test_scanner.py`

- [ ] **Step 1: 失败测试**（conftest 提供 `make_photo(path, format, **exif)` helper：用 Pillow 造图；HEIC 用 pillow_heif；RAW 用假 `.dng` 无法解码走失败分支）：
  - 新目录扫描 → files 行数/格式正确；再扫无变化 → 不重复插入
  - 修改 mtime → 重哈希更新行
  - RAW+JPG 同基名（`IMG_1.CR3`+`IMG_1.JPG`）→ 同一 photo，primary=RAW
  - 时间差 1.5s 但不同名同目录 → 也配对；时间差 30s 不配对
  - EXIF：DateTimeOriginal → captured_at；Make/Model → camera

- [ ] **Step 2: 验证失败** → **Step 3: 实现**：
  - `SUPPORTED = {'.jpg': 'jpg', '.jpeg': 'jpg', '.heic': 'heic', '.heif': 'heic', '.png': 'png', '.cr2': 'raw', ...}`（rawpy 支持的全集）
  - `scan(conn, progress_cb=None) -> ScanReport`：os.walk 枚举 → 与库中 (path,mtime,size) 比对 → 新/变更文件算 md5、抽 EXIF（`pillow_heif.register_heif_opener()` 一次）、建 photo → 删除的行标记保留但 photo 不再入组 → RAW/JPG 配对 `pair_raw_jpg(conn)`（同目录同名或同目录 Δt<2s）
  - `extract_exif(path) -> dict`：Pillow `_getexif`/`getexif()`，EXIF tag 映射（306=DateTimeOriginal, 271/272=Make/Model, 33434/33437/34855/37386 = 曝光时间/光圈/ISO/焦距）；RAW 先试 `rawpy`（无 EXIF 接口则读内嵌预览 Pillow 解析），失败 → {}（文件仍入库，format=raw，后续质量分析跳过）
  - captured_at 解析失败 → 用 mtime 兜底

- [ ] **Step 4: 通过 + commit** `feat: scanner with incremental index and raw-jpg pairing`

### Task 3: hashing.py + grouper.py

**Files:**
- Create: `backend/app/hashing.py`、`backend/app/grouper.py`、`backend/tests/test_grouper.py`

- [ ] **Step 1: 失败测试**：
  - `phash_distance(a, b)` 对同图=0；模糊/亮度变化副本距离 <10；完全不同照片距离 >20
  - 时间相邻+phash 近 → 一组；时间相邻但内容不同 → 两组
  - 三张链式相似（A~B、B~C、A≁C）→ union-find 并成一组
  - 已配对 RAW+JPG 只出现一个 photo
  - `dismiss_group` → status=not_duplicate
  - BK-tree：500 张合成哈希检索 <1s

- [ ] **Step 2: 验证失败** → **Step 3: 实现**：
  - `hashing.py`：`compute_hashes(conn, progress_cb)` 对每 file 用 Task5 的 `decode(path, max_side=256)`（先内嵌预览）算 imagehash 的 phash/ahash/dhash（hex 存 files 表 hash_phash 等 3 列）；解码失败留空
  - `grouper.py`：`build_groups(conn)`：
    1. SQL 按 captured_at 排序，滑动窗口 Δt<time_window_sec 生成时间候选对
    2. BK-tree（dict 递归结构，节点 {hash, children: {distance: node}}）全量汉明距离 ≤threshold 检索（覆盖跨时间同场景）；候选相似判定：phash ≤ thr，且 (thr-4 ≤ phash < thr) 时要求 ahash+dhash 至少一个也 ≤thr（投票复核）
    3. Union-find 成组，写入 groups/group_id，组大小≥2 才建组；清空旧组重算（幂等）
  - `dismiss_group(conn, group_id)`

- [ ] **Step 4: 通过 + commit** `feat: phash grouping with bk-tree and union-find`

### Task 4: quality/（5 维度）+ jobs.py

**Files:**
- Create: `backend/app/quality/__init__.py`（`analyze_photo(path) -> dict` 调度）、`quality/sharpness.py`、`quality/exposure.py`、`quality/noise.py`、`quality/composition.py`、`quality/faces.py`、`backend/app/jobs.py`、`backend/tests/test_quality.py`

- [ ] **Step 1: 失败测试**（程序化生成测试图：Lena 类灰度图/渐变）：
  - 清晰 vs 高斯模糊 σ=3 → sharpness 分差 >25
  - 过曝（+80）→ exposure 分 <50 且 details 含 clipped_high 比例 >0.2
  - 暗部强噪声图 → noise 分显著低于干净图
  - 旋转 5° 的水平线图 → composition 分降且 details.tilt_deg≈5
  - faces：无人脸 → faces=None；`face_engine='none'` → None
  - jobs：submit/轮询状态/进度回调收敛

- [ ] **Step 2: 验证失败** → **Step 3: 实现**：
  - 公共：`decode_for_analysis(path, max_side=2048)`（Task5）；分数全部归一化 0–100（越高越好）
  - `sharpness.py`：`cv2.Laplacian(gray, cv2.CV_64F).var()`，中心 50% 区域与全图两个值取 min 后 log 归一化（参考锚点：var 1000→≈90，var 10→≈10）
  - `exposure.py`：256-bin 直方图 → clipped_high/low（>250/<5 像素比）、均值偏离 127 的罚分、动态范围加分
  - `noise.py`：取亮度<80 的暗部块，`cv2.Laplacian` 高频能量/均值（无暗块时全图估）
  - `composition.py`：梯度方向直方图峰值 → 主导方向与水平的夹角 tilt_deg；水平<1.5° 满分，线性衰减到 10° 零分
  - `faces.py`：`face_engine='auto'` → 尝试 import mediapipe FaceMesh 计算每眼 EAR（闭合阈值 0.20）；失败或 'haar' → cv2 自带 haarcascade_frontalface + eye（检测出脸但检测不到睁眼→记 closed）；结果 `{faces: n, closed_eyes: n, engine: 'mediapipe'|'haar'}`，faces 分 = 100 - 40*(closed_eyes/eyes_total)（无脸 None）
  - `jobs.py`：`JobManager`（线程 + concurrent.futures.ProcessPoolExecutor(max_workers=cpu-1)）：`submit_scan/submit_analysis/submit_execute`，任务状态字典 {id, phase, progress, error}，`status()` 供轮询；analysis 阶段把 quality_scores 写库

- [ ] **Step 4: 通过 + commit** `feat: quality analysis pipeline with process pool`

### Task 5: imaging.py（解码/缩略图/预览缓存）

**Files:**
- Create: `backend/app/imaging.py`、`backend/tests/test_imaging.py`

- [ ] **Step 1: 失败测试**：JPG/HEIC/PNG 解码为 RGB、max_side 正确；RAW（真实 DNG fixture 太重 → mock rawpy 返回 ndarray）；`get_image(conn, photo_id, max)` 三档缓存命中不重复解码（mtime 比对）；LRU 超限删除最旧。

- [ ] **Step 2: 验证失败** → **Step 3: 实现**：
  - `decode(path, max_side) -> PIL.Image`（模块级注册 heif opener；raw → rawpy.postprocess(half_size=True, no interpolation needed at scale) → Pillow；rawpy 失败 → 内嵌预览再失败 → raise DecodeError）
  - `get_thumb(conn, file_id, size=256)` / `get_preview(conn, file_id, max_side)`：缓存 `data/thumbs/{file_id}_{size}.webp`、`data/previews/{file_id}_{max}.jpg`；目录惰性创建；缓存 key 含 mtime 防陈旧；`enforce_cache_limit()`（LRU by atime，2GB 上限，预留 10%）
  - 供 Task3/4 复用：`decode_for_analysis`、内嵌预览提取 `raw_embedded_preview(path)`

- [ ] **Step 4: 通过 + commit** `feat: image decode and cache pipeline`

### Task 6: recommender.py

**Files:**
- Create: `backend/app/recommender.py`、`backend/tests/test_recommender.py`

- [ ] **Step 1: 失败测试**：
  - 权重均匀时：清晰度 92 vs 78 → 推荐前者；理由文本包含「清晰度」「92」「78」
  - RAW+JPG 同分 → 推荐该 photo 的 RAW asset（理由提「保留 RAW 底片」）
  - 无显著差距（全维 Δ<5）→ 中性理由「各维度表现接近，已按当前偏好排序」
  - 权重变化后（清晰度→0.7）排序翻转 → 推荐变化
  - faces=None 不参与求和（维度缺失时权重重归一化）

- [ ] **Step 2: 验证失败** → **Step 3: 实现**：
  - `recommend_group(conn, group_id, weights) -> {recommended_photo_id, reason, ranked: [...]}`：综合分 = Σ(dim_score × w_dim) / Σw；理由：Δ≥10 的优势维度列「清晰度 92 vs 78」前 2 项 + 通用尾注；写入 groups.recommended_photo_id
  - `DEFAULT_WEIGHTS = {sharpness: 1.3, exposure: 1.0, noise: 0.8, composition: 0.9, faces: 1.2}`（摄影常识先验）

- [ ] **Step 4: 通过 + commit** `feat: recommender with reason generation`

### Task 7: learner.py

**Files:**
- Create: `backend/app/learner.py`、`backend/tests/test_learner.py`

- [ ] **Step 1: 失败测试**：
  - 合成：偏好曝光>清晰度的用户做 30 组决策（保留曝光高分者）→ 训练后 w_exposure > w_sharpness
  - EMA：旧权重 [1,1,...]，新 [0,1,...]，样本数 5 → 混合结果在两者之间；样本数 200 → 贴近新
  - 显式反馈 dislike 一张（清晰度极高但构图差）→ 训练后 w_composition 升
  - not_duplicate 组不产生样本
  - 保存后 `get_current_weights()` 可读，版本 +1

- [ ] **Step 2: 验证失败** → **Step 3: 实现**：
  - `record_decision_samples(conn, group_id, kept_ids)`：kept×rejected 笛卡尔积 → (Δfeatures, 1) 正样本；rejected×kept → 0（共 2n 对）
  - 显式反馈：dislike photo X（上下文组内有 kept）→ 注入 3 倍权重样本
  - `retrain(conn)`：全部样本 → `scipy.optimize.minimize` L-BFGS 求 pairwise logistic loss（L2 λ=0.1×√n）；特征 6 维（faces 缺失填中性 50，锁定维度权重固定不优化）；EMA 混合 `β = 0.5/(1+n/50)`；写 preference_model 新版本
  - `get_current_weights(conn) -> {weights, sample_count, locked: []}`

- [ ] **Step 4: 通过 + commit** `feat: pairwise preference learner`

### Task 8: executor.py

**Files:**
- Create: `backend/app/executor.py`、`backend/tests/test_executor.py`

- [ ] **Step 1: 失败测试**：
  - 未保留 photo 的 RAW+JPG 都移动到 `<rejected_root>/<原相对路径>`，源消失
  - 目标已存在同名 → `_1` 后缀，源内容不覆盖
  - `undo_group` → 全部原路移回，move_log.undone=1
  - 决策含全部组员（保留全部）→ 无移动
  - 跨子目录结构保持（a/b/IMG.jpg → rejected_root/a/b/IMG.jpg）

- [ ] **Step 2: 验证失败** → **Step 3: 实现**：
  - `apply_decision(conn, group_id, keep_photo_ids) -> decision_id`：写 decisions、调 learner.record_decision_samples + retrain、jobs.submit_execute(move_ops)
  - `move_ops`：对每个未保留 photo 的所有 files：`shutil.move`（同盘 rename/跨盘 copy+unlink，shutil 自动处理）→ move_log；失败 → 队列保留可重试（错误写 scan_runs.error 风格日志）
  - `undo_group(conn, group_id)`：按 move_log 逆移回，标记 undone
  - rejected_root 默认值计算 `default_rejected_root(conn)`：`<第一个源目录>/../_photo_picker_rejected`

- [ ] **Step 4: 通过 + commit** `feat: safe move executor with undo`

### Task 9: API 层 + main.py

**Files:**
- Create: `backend/app/main.py`、`backend/app/api/__init__.py`、`backend/app/api/routes_scan.py`、`routes_settings.py`、`routes_groups.py`、`routes_preferences.py`、`backend/tests/test_api.py`

- [ ] **Step 1: 失败测试**（httpx TestClient + tmp db fixture，monkeypatch 后台任务同步执行）：完整流程 scan→groups list→group detail（含评分/推荐/理由）→decision（权重变化）→undo→dismiss→feedback→photo image 返回 200 且 content-type image/jpeg→preferences GET/PUT。

- [ ] **Step 2: 验证失败** → **Step 3: 实现**：
  - 端点按 spec §6；`GET /groups/{id}` 返回 members: [{photo_id, primary_file_id, kept, scores{...}, exif, is_raw}] + recommendation{photo_id, reason}
  - `GET /photo/{id}/image?max=`：256|1024|0（0=原图转码，上限 8192）→ FileResponse/StreamingResponse，ETag=file_hash
  - main.py：lifespan 初始化 db + jobs；生产模式挂 `frontend/dist` 静态；`python -m app.main` → uvicorn.run(host=127.0.0.1, port=8765, open browser)
  - CORS：dev 模式允许 vite 端口

- [ ] **Step 4: 通过 + commit** `feat: fastapi routes and app entry`

### Task 10: 前端骨架 + 设置/扫描页

**Files:**
- Create: `frontend/`（Vite react-ts 模板：package.json、vite.config.ts（proxy /api→8765）、tsconfig、index.html、src/main.tsx、src/App.tsx（react-router-dom）、src/api.ts（fetch 封装+类型）、src/pages/SettingsPage.tsx、src/components/ScanProgress.tsx、src/types.ts）

- [ ] **Step 1: 实现**：SettingsPage = 源目录列表（输入路径+校验存在+增删）、阈值滑块（4–16 显示「严格↔宽松」）、时间窗口、rejected 路径、「开始扫描」→ 轮询 /scan/status 显示阶段进度条（扫描→哈希→分组→分析→完成，含错误红条）
- [ ] **Step 2: 手动验证**：`pnpm dev` + 后端跑通扫描（用小测试目录）
- [ ] **Step 3: commit** `feat: frontend skeleton with settings and scan page`

### Task 11: 分组总览页 + 对比页

**Files:**
- Create: `src/pages/GroupsPage.tsx`、`src/pages/ComparePage.tsx`、`src/components/SyncViewer.tsx`、`src/components/ScoreBars.tsx`、`src/components/ExifBar.tsx`、`src/components/ReasonCard.tsx`

- [ ] **Step 1: GroupsPage**：组网格卡片（推荐图 thumb + 张数徽标 + 状态），pending/decided 筛选 + 分页；顶部「已处理 X/Y 组 · 预计可省 Z MB」
- [ ] **Step 2: SyncViewer（核心）**：N 张图共享 {scale, offset}，useRef 存每张 natural size；wheel 以鼠标为锚缩放（clamp 0.1–8）、拖拽平移、双击复位；显示百分比（100% 基准 = 1:1 像素）；图片用 /photo/{id}/image?max=1024（>100% 时切 ?max=0）
- [ ] **Step 3: ComparePage**：组内 photo 卡（SyncViewer 项 + ExifBar（快门/光圈/ISO/焦距/时间/相机）+ ScoreBars（5 维横条+分数+高亮推荐者优势维）+ 保留勾选 + 👍/👎）+ ReasonCard + 快捷键（useKey hooks：数字 1–4 切保留、Enter 确认→POST decision→跳下一 pending、←/→ 组导航、N=dismiss）；确认后 learner 重训的权重变化 toast 提示（「已学习：曝光权重 1.0→1.3」）
- [ ] **Step 4: 手动验证 + commit** `feat: groups overview and sync-compare page`

### Task 12: 偏好面板 + 历史/撤销页

**Files:**
- Create: `src/pages/PreferencesPage.tsx`、`src/pages/HistoryPage.tsx`

- [ ] **Step 1: PreferencesPage**：权重横条（0–2 刻度）+ 样本数 + 「学习中/已稳定」状态 + 每维锁定开关 + 手动滑块（PUT /preferences）
- [ ] **Step 2: HistoryPage**：已处理组列表（决策时间、保留张、推荐是否命中）+ 撤销按钮（POST undo，乐观更新）
- [ ] **Step 3: 验证 + commit** `feat: preferences panel and history page`

### Task 13: run.sh + 端到端验收

- [ ] **Step 1: `run.sh`**：`pnpm --dir frontend build`（存在 dist 时跳过）→ `.venv/bin/python -m app.main`（自动开浏览器 http://127.0.0.1:8765）
- [ ] **Step 2: 端到端**：真实小照片目录（含连拍、RAW+JPG、HEIC）走完：扫描→分组检查→对比页选择 5+ 组→验证权重变化→淘汰移动→访达确认 rejected 结构→UI 撤销一组→确认回原位
- [ ] **Step 3: 全测试回归 + commit** `feat: one-command launch and e2e validation`

---

## Self-Review 记录

- Spec 覆盖：需求表 12 项 → Task 1-13 全覆盖（学习→T7+T12；淘汰→T8+T12；对比体验→T11）
- 类型/命名一致性：scan/decision/undo/dismiss/feedback/preferences 端点名与 spec §6 一致；weights 维度名 5 个全程一致（sharpness/exposure/noise/composition/faces）
- 无占位符；测试均为可执行断言
