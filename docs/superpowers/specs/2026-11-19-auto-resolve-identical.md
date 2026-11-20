# 自动处理完全相同照片（auto-resolve identical）

日期：2026-11-19 | 状态：已实现

## 需求（用户原话，m03248）

「如果两张照片内容完全一样，只是文件名不一致，比如 2020-12-19 111213.heic 和 2020-12-19 111212(1).heic，请自动保留 2020-12-19 111212.heic，又比如 2020-12-28 004344.heic 和 15423728636290139782.jpg，请保留 2020-12-28 004344.heic，即直接保留原片，不要再让我决策了，直接帮我按照这个规则处理，在页面里给出自动处理组数即可」

## 判定标准（用户已确认）

**仅三哈希全同自动**：pHash、aHash、dHash 三个距离全部为 0（像素级完全相同）。用户库实证 2844 对。
连拍近似（例 1 的 111213/111212(1)，phash 距离 4-6）**不自动处理**——保留哪张涉及质量判断，仍由用户决策（推荐已带副本后缀/乱码名惩罚，80edc18）。

## 保留（keeper）规则链

确定性排序，不掺学习权重（自动规则可解释）：
1. 文件名无副本后缀 `\(\d+\)`（recommender._is_copy_suffix）
2. 文件名规律性高（recommender._filename_regularity：日期名 1.0 > 序列名 0.9 > 普通名 0.5 > 乱码 0）
3. primary file 有 captured_at（EXIF）
4. RAW 格式
5. 质量分 total 高
6. photo_id 小

## 机制：剥离成新组再决策（复用组级语义）

不对原组动刀（组内可能混有连拍近似，部分淘汰会让 pending 组语义破碎）：
1. 扫 pending 组内三哈希全同 cluster（union-find）
2. 每个 cluster>1：**新建 pending 组**，把 cluster 全部 photos 挂到新组
3. 对新组调 `executor.apply_decision(新组, [keeper], learn=False, source='auto')`——decided/文件移动/撤销/历史全部复用现有组级机制
4. 原组保留剩余照片（pending，继续用户决策）；剩 0/1 张时下次 build_groups 自然消散

## 学习语义

`learn=False`：自动决策**不产生偏好样本**（规则不是用户偏好信号）。decisions 行照写（历史/撤销需要）。

## 变更点

- db.py：decisions 加 `source TEXT NOT NULL DEFAULT 'user'` 列 + init_db 轻量 migration（PRAGMA table_info 检测）
- executor.py：apply_decision 加 `source="user"` 参数写 decisions 行
- auto_resolve.py（新）：find_identical_clusters / pick_keeper / auto_resolve_pending(conn, progress_cb, enabled)
- config.py：`auto_resolve_identical: True`（bool，设置页开关）
- api.py：pipeline 第六阶段 auto（cb('auto', i, n)+报告 auto 键）；overview 加 auto_resolved_groups；listHistory 决策行加 source；PUT /api/settings（白名单键）
- main.py：PUT /api/settings 路由
- 前端：settings.js 六阶段进度+开关+报告行；history.js「自动」徽章；overview.js 统计卡「自动处理」

## 幂等性

处理后新组 decided 不会被重扫；撤销后组回 pending，重跑流水线会重新 auto（行为一致）。
