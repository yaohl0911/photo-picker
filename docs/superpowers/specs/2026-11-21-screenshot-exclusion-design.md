# 截图退出重复照片流程设计

日期：2026-11-21 · 状态：已获用户批准（方案 A）

## 背景与决策

- 用户需求（原文）：「截图不要出现在重复照片中了，单独处理」。
- 现状根因：截图判定是**惰性**的——只有截图页 API 被调用时才分类（`screenshots.ensure_analyzed`），流水线从不分类。`grouper.build_groups` 的候选 SQL 只看 `p.group_id IS NULL AND f.hash_phash != ''`，无截图过滤，未分类截图（`is_screenshot IS NULL`）直接进入相似分组；界面相似的截图 pHash 距离小，天然聚成组，混进「重复照片」待决策队列；第 6 阶段「自动去重」也会顺带处理组内相同像素截图。
- 用户拍板：截图**彻底退出所有去重流程**（含像素级相同截图的自动去重），完全交给截图页批量处理。

## 方案（A：流水线急切分类 + 两处 SQL 过滤）

弃选方案：B 在扫描阶段分类（耦合 scanner、每个文件都要开图读头、独立扫描被迫做无关工作）；C 仅 API/前端过滤展示（数据层面截图仍在组内，计数、自动去重、keeper 选择仍漏）。

### 改动 1：流水线插入截图识别阶段

`backend/app/api.py` `run_full_pipeline_blocking`：在感知哈希之后、相似分组之前调用 `screenshots.ensure_analyzed(conn)`：

- 流水线变为 7 阶段：`scan → hash → screenshots → group → analysis → recommend → auto`；
- `report["screenshots"] = {"scanned": …, "found": …}` 进入流水线报告（additive，兼容现有消费者）；
- `ensure_analyzed` 增加可选 `progress_cb`，阶段名 `screenshots`，上报已分类行数，驱动设置页进度条。

### 改动 2：相似分组排除已知截图

`backend/app/grouper.py` 候选 SQL 增加条件：

```sql
AND COALESCE(p.is_screenshot, 0) = 0
```

**NULL（未分类）按照片处理**——保守兜底：识别规则覆盖不到的截图（冷门机型尺寸）仍走去重流程，宁可误入也不漏真照片。

### 改动 3：自动去重排除已知截图

`backend/app/auto_resolve.py` `find_identical_clusters` 的 pending 组扫描 SQL 加同样过滤——把历史遗留 pending 组里的截图也清出自动去重（含像素级相同者，按用户拍板不再自动处理）。

### 改动 4：前端进度条补阶段

`frontend/dist/js/pages/settings.js`：`STAGE_LABELS` 与 `PIPELINE_STAGES` 在「感知哈希」后插入 `{ key: 'screenshots', label: '截图识别' }`。

## 行为边界（刻意不变 / 固化语义）

- **已决策的组不动**：decided / not_duplicate 组与组内照片保持原样，历史决策记录一致。
- **pending 组的清理走既有重建语义**：无决策历史的 pending 组下次流水线被解散重建，截图自然出队；**有撤销历史的 pending 组保留原样**（撤销语义 = 用户要回这组，优先级高于本次排除）。
- **相同像素截图不再自动去重**：只能出现在截图页，由用户批量处理。
- `ensure_analyzed` 幂等且增量（`WHERE is_screenshot IS NULL`）：流水线反复跑只处理新增文件；截图页的惰性分类保留（首次访问兜底）。
- 截图页 `reject_screenshots` 的瞬态组机制不变（组创建后立即决策，auto 阶段只会看到 pending，无交叉）。
- 不新增用户设置项；`update_settings` 白名单保持 `{"auto_resolve_identical"}`。

## 验收标准

1. 流水线跑完后，待决策组中不存在 `is_screenshot = 1` 的照片（用测试库断言）。
2. 截图（含像素级相同者）不会被自动去重阶段处理；`report["screenshots"]` 出现在流水线报告。
3. 未分类（NULL）照片仍正常进入相似分组（兜底不被误伤）。
4. 设置页进度条显示 7 个阶段，「截图识别」位于感知哈希之后。
5. 现有 pytest 测试全部通过，新增测试覆盖上述 1–3。
