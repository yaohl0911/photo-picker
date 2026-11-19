# 照片去重助手

本地优先的照片去重与偏好学习工具：扫描相册目录，用感知哈希找出相似照片组，基于质量分析给出「保留哪张」的推荐和理由，你在并排对比中做决策，每一次决策都会反哺偏好权重，越用越懂你。

## 功能特性

- **目录扫描**：添加多个源目录，后台扫描建立索引，支持 JPG / HEIC / RAW（CR3 / ARW / NEF / RAF / DNG / ORF / RW2），RAW+JPG 自动配对，文件变更增量更新。
- **相似分组**：pHash 感知哈希 + 拍摄时间窗联合判定，把「同一场景的连拍/多机位照片」聚成待决策组。
- **推荐 + 理由**：对每组给出推荐保留项及理由（清晰度、分辨率、曝光、构图等质量维度），告诉你为什么是它。
- **多选决策**：对比页可保留一张或多张，快捷键 1-9 直接选、Enter 确认、D 标记淘汰方向、←→ 在候选间切换。
- **淘汰可撤销**：非保留照片移入淘汰目录 `_photo_picker_rejected`（保持原目录结构，文件永不删除），历史页可随时整组撤销。
- **偏好学习闭环**：每个决策记录维度权重反馈，偏好页可视化权重变化，支持微调、锁定维度、重新训练排序模型。

## 快速开始

### 环境要求

- macOS（淘汰目录移动、浏览器唤起等按 macOS 设计）
- Python 3.12+

### 安装

```bash
uv venv                                   # 创建 .venv（或 python3 -m venv .venv）
uv pip install -r requirements.txt        # 或 .venv/bin/pip install -r requirements.txt
```

### 启动

```bash
./run.sh
```

脚本会以仓库根为工作目录启动后端（`backend.app.main`），并在 1.5 秒后自动打开浏览器。若没有自动打开，手动访问 **http://127.0.0.1:8765** 即可。停止服务按 `Ctrl-C`。

## 使用流程

1. **设置页**：添加照片目录（绝对路径），点击「保存并扫描」。目录列表会从服务端加载，浏览器 localStorage 仅作离线备份；扫描有实时进度条，完成后统计卡片即时刷新。
2. **总览页**：查看图库统计与待决策组列表，进入任意一组。
3. **对比页**：SyncViewer 并排查看同组照片——**拖动同步平移、滚轮同步缩放、双击复位**，放大对齐细节逐项对比。点击卡片选择保留项（可多选），快捷键 `1-9` 直接编号选择、`Enter` 确认、`D` 标记淘汰方向、`←→` 在候选间切换。
4. **确认决策**：确认后自动进入下一组，非保留照片移入淘汰目录（可撤销）。
5. **偏好页**：查看各质量维度的权重变化曲线，微调权重、锁定不想被动到的维度、一键重新训练。
6. **历史页**：回看已做的决策，整组撤销误判，照片原样移回。

## 数据说明

- `data/photo_picker.db` —— SQLite 数据库：文件索引、哈希、质量分析、相似分组、推荐结果、决策历史、偏好权重。删除该文件即可完全重置。
- `data/` —— 缩略图等派生缓存。
- `<每个照片文件夹>/_photo_picker_rejected/` —— 淘汰目录（默认分散模式：各照片文件夹内部的 `_photo_picker_rejected`，互不混杂，内部结构与该文件夹平行；显式设置 `rejected_root` 则集中到一处）：移入的照片**永不删除**，历史页可撤销恢复。扫描器会自动跳过该目录名，不会重复索引。**同名伴随文件一起迁移**（Live Photo 的 `IMG_123.MOV`、编辑侧车 `IMG_123.xmp`、DJI 字幕 `DJI_123.SRT` 等同 stem 的非索引文件），撤销时同步还原；视频/侧车本身不参与索引和去重，仅做伴随迁移。

## 开发

### 目录结构

```
backend/
  app/
    main.py        # FastAPI 路由层（薄适配）+ uvicorn 启动入口
    api.py         # 服务层：纯 Python，sqlite3 连接为第一参数，返回 JSON-ready dict
    db.py          # SQLite 连接/迁移
    scanner.py     # 目录扫描与索引增量更新
    hashing.py     # pHash 感知哈希
    grouper.py     # 相似分组
    imaging.py     # 图像解码（JPG/HEIC/RAW）
    quality/       # 质量分析（清晰度/曝光/构图等）
    recommender.py # 推荐 + 理由
    learner.py     # 偏好学习（权重反馈/训练）
    executor.py    # 决策执行与撤销（淘汰移动）
    jobs.py        # 后台作业管理
    config.py      # settings 键值存取
  tests/           # pytest 测试
frontend/dist/     # 静态前端（原生 ES 模块 SPA，hash 路由）
run.sh             # 一键启动
requirements.txt   # 依赖锁定
docs/DEVENV.md     # 开发环境说明
```

### 跑测试

```bash
.venv/bin/python -m pytest backend/tests -v
```

### DSH 双轨约束

本仓库在两套环境开发：DSH 沙箱内 fastapi/pydantic 等原生库无法导入（只能跑服务层纯 Python 测试），用户终端才能跑完整套件与真实服务。详见 [docs/DEVENV.md](docs/DEVENV.md)。

## 已知限制

- 目前仅针对 macOS 设计（淘汰移动策略、浏览器唤起）。
- 混合开发环境（DSH 沙箱 + 用户终端）下 `.venv` 内 `python3.12` 为重签副本，属环境残留而非项目问题——说明见 [docs/DEVENV.md](docs/DEVENV.md)。
- 相似判定基于 pHash 阈值与时间窗，极端场景（大范围裁剪、色调剧变）可能漏判。
