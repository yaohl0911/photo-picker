# 照片去重与偏好学习工具（photo-picker）设计文档

日期：2026-11-18
状态：已确认（经三轮需求问答 + 计划批准）

## 1. 目标

为摄影爱好者构建**本地 Web 应用**：扫描指定文件夹 → 识别连拍/同场景的近似重复照片 → 每组推荐一张最佳照片并给出具体推荐理由 → 用户多轮选择后系统学习其偏好，推荐越来越准；淘汰照片安全移入专门文件夹，可撤销。

## 2. 已确认需求

| 决策点 | 确认结果 |
|---|---|
| 照片来源 | 指定文件夹递归扫描（多目录） |
| 相似界定 | 连拍/同场景近似；相似度阈值可调 |
| 产品形态 | 本地 Web 应用（FastAPI 后端 + 浏览器 UI） |
| 图库规模 | 1,000–10,000 张 → 需哈希/缩略图缓存与增量扫描 |
| RAW+JPG | 同场景合并配对为同一逻辑照片，默认推荐保留 RAW |
| 质量维度 | 清晰度/锐度、曝光/亮度、人像闭眼/表情、构图/水平线、噪点（全部） |
| 学习方式 | 隐式学习为主 + 可选显式👍/👎 + 权重查看/微调面板 |
| 淘汰策略 | 移入「淘汰」文件夹（保持原目录结构），可恢复，永不真删 |
| 格式 | JPG、HEIC/HEIF、RAW（CR2/CR3/NEF/ARW/DNG 等主流）、PNG |
| 对比体验 | 同步缩放并排对比（100% 像素级）、EXIF 并排、质量评分条、快捷键 |
| 保留规则 | 每组推荐 1 张，允许自选保留多张；保留集合都作为学习正样本 |
| 软件互通 | 本期不做 XMP，架构预留扩展点 |

## 3. 技术架构

- **后端**：Python 3.12 + FastAPI + uvicorn + SQLite（标准库 sqlite3）+ imagehash + OpenCV + Pillow/pillow-heif + rawpy + scipy + pytest。零新增后端依赖（可选 mediapipe，失败自动降级 OpenCV Haar）。
- **前端**：React + Vite + TypeScript，`frontend/` 目录，构建产物由 FastAPI 托管。
- **数据**：`data/app.db`（SQLite）+ `data/thumbs/`、`data/previews/`（图像缓存，LRU 上限 2GB）。

**方案选择理由：**
- 相似度：pHash + 拍摄时间聚类 + BK-tree 检索，aHash/dHash 投票复核。否决 CLIP（连拍级用不上语义、太重）与逐像素比对（对微差不鲁棒）。
- 偏好学习：线性 pairwise 模型（Bradley-Terry / pairwise logistic regression）。可解释、小样本高效；否决黑盒模型（与「给出推荐原因」冲突）。
- 存储：SQLite。否决 PostgreSQL（单机单用户无需服务进程）。

## 4. 数据模型（SQLite，schema 版本化）

```
files           物理文件: id, path(唯一), mtime, size, file_hash(md5), format, photo_id, captured_at, camera, exif_json
photos          逻辑照片: id, primary_file_id(默认RAW), group_id
groups          相似组: id, status(pending|decided|not_duplicate), recommended_photo_id, created_at
quality_scores  photo_id, sharpness, exposure, noise, composition, faces, details_json   (0–100)
decisions       group_id, kept_photo_ids(json), decided_at, applied_at
feedback_events photo_id, action(like|dislike), group_id, created_at
preference_model version, weights_json, bias, sample_count, trained_at
move_log        decision_id, src_path, dst_path, moved_at, undone
scan_runs       id, started_at, finished_at, phase, progress, error
```

## 5. 模块设计

### scanner
递归枚举源目录；扩展名识别格式；md5+mtime/size 增量；EXIF（Pillow 处理 JPG/HEIC，rawpy 或内嵌预览处理 RAW）；RAW+JPG 配对：同目录同名，或同目录+拍摄时间差<2s+分辨率相近 → 合并 photo。

### hashing
pHash/aHash/dHash（输入最长边 256px 灰度图，RAW 用内嵌预览）。结果落库复用。

### grouper
1. 候选对：拍摄时间相近（默认 <60s）聚簇 + pHash BK-tree 汉明距离检索兜底（覆盖三脚架间隔拍摄）。
2. 相似判定：pHash 汉明距离 ≤ 阈值（默认 10/64，UI 可调 4–16）；边界用 aHash+dHash 投票复核。
3. union-find 成组；用户可标记 not_duplicate（解散且不参与学习）。

### quality（进程池并行）
- 清晰度：Laplacian variance（中心/全图双区）
- 曝光：直方图过曝/欠曝像素比例、均值、动态范围
- 噪点：暗部小块高频能量
- 构图/水平线：梯度方向直方图估计倾角
- 人脸闭眼：mediapipe FaceMesh EAR 判闭眼；降级 Haar 人脸/眼睛级联（UI 显示「基础模式」）；无脸组跳过
- 全部归一化 0–100 入库；分析图与 pHash 输入共用一次解码

### recommender
各维度分 × 偏好权重 → 综合分；组内最高为推荐（RAW/JPG 同 photo 默认指 RAW）；理由生成：找显著优势维度（差距>阈值才提），拼装中文理由+具体分数对比。

### learner
- 样本：决策产生 pairwise (保留, 淘汰) 对，特征 = 维度分数差
- pairwise logistic regression（scipy L-BFGS + L2），特征 6 维 → 权重映射偏好面板；冷启动先验：清晰度略高
- 平滑：新权重与旧权重 EMA 混合（β 随样本数增大而减小）
- 显式👍/👎 → 高权重 pairwise 样本；面板可手动锁定维度

### executor
未保留 photo 的全部 files 移到 `<rejected_root>/<原相对路径>`（默认 `<第一个源目录>/../_photo_picker_rejected/`，可配置）；冲突加 `_1` 后缀；move_log 支持按组撤销；后台队列 + 进度；**永不删除**。

### imaging
HEIC/RAW 后端转码 JPEG：256px 列表 / 1024px 对比 / 原图级 100% 检查（`?max=0`）；缓存 thumbs/ + previews/（LRU 2GB）。

### jobs
后台任务（扫描/分析/移动）统一队列：阶段、进度、错误上报；可中断恢复。

## 6. API 契约（前缀 /api）

```
POST /scan/start                       GET /scan/status
GET /settings                          PUT /settings（源目录/阈值/rejected_root）
GET /groups?status=&page=              GET /groups/{id}
POST /groups/{id}/decision             {keep_photo_ids}
POST /groups/{id}/dismiss              （标记非重复）
POST /groups/{id}/undo                 （撤销移动）
POST /feedback                         {photo_id, action, group_id}
GET /preferences                       PUT /preferences（微调/锁定）
GET /photo/{id}/image?max=256|1024|0   GET /photo/{id}/exif
```

## 7. 前端（React + Vite + TS）

1. **设置/扫描页**：源目录管理、阈值滑块、扫描进度（阶段条）
2. **分组总览页**：组网格卡片（推荐缩略图+张数+状态），筛选，已省空间统计
3. **对比页（核心）**：2–4 张并排、同步缩放平移（共享 scale/offset、滚轮锚点缩放、拖拽、双击复位、100% 标记）、EXIF 条、质量评分条、推荐徽标+理由卡、保留勾选、👍/👎、快捷键（数字键保留、Enter 确认跳下一组、←/→ 组导航、N 标记非重复）
4. **偏好面板**：权重可视化、样本数、趋势、手动微调/锁定
5. **历史/撤销页**：已处理组列表、撤销

启动：开发 uvicorn + vite dev；日常 `./run.sh` 一键构建+启动+开浏览器。

## 8. 边界情况

- 分析中文件变动 → 跳过并报告，不崩溃
- 同内容不同路径 → file_hash 相同合并 photo；同名不同内容 → md5 区分
- RAW 解码失败 → 条目保留标记「无法分析」，可手动保留不参与推荐
- 中途关闭页面 → 决策实时入库，断点续用
- not_duplicate 组决策不进训练集
- 跨盘移动耗时 → 后台队列、失败可重试

## 9. 测试与验收

- pytest：scanner（增量/配对）、grouper（阈值/BK-tree/union-find）、quality（程序化生成清晰/模糊/过曝图验证分数排序）、recommender（理由含具体数字）、learner（合成偏好数据权重恢复+EMA）、executor（移动/冲突/撤销）+ httpx TestClient 端到端流程
- 最终验收：真实照片目录跑通「扫描→分组→推荐→选择→学习→淘汰→撤销」

## 10. 里程碑

1. db/schema + config + scanner
2. hashing + grouper
3. quality 管线 + jobs
4. recommender
5. API + imaging
6. 前端骨架 + 设置/扫描 + 分组总览
7. 对比页
8. executor + 撤销 + learner + 偏好面板
9. 一键启动 + 端到端验收

## 11. 本期范围外

Apple 照片图库、XMP sidecar、严格逐像素模式、桌面打包、云同步（架构已预留：files/photos 解耦、decision 事件化）。
