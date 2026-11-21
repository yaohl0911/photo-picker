# 截图退出重复照片流程 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 截图（`photos.is_screenshot = 1`）彻底退出重复照片流程：不进相似分组、不被自动去重处理（含像素级相同者），只在截图页出现。流水线在分组前急切分类（新增 `screenshots` 阶段），识别规则覆盖不到的（NULL 未分类）保守按照片处理。

**Architecture:** 两个 SQL 条件（`grouper.py` 候选池、`auto_resolve.py` pending 扫描）+ 流水线插入 `screenshots.ensure_analyzed` 步骤（带进度回调）+ 前端进度条补一行阶段标签。改动全部是过滤与编排，不碰决策/撤销/偏好学习逻辑。设计依据：`docs/superpowers/specs/2026-11-21-screenshot-exclusion-design.md`（已获用户批准）。

**Tech Stack:** 既有栈；pytest（`backend/tests/`，DSH 环境跑 `.venv/bin/python -m pytest`），前端无自动化测试（grep + 人工目检）。

**刻意不碰（重要）：**
- `screenshots.py` 的分类规则（`_classify`、`SCREEN_SIZES`）与截图页惰性分类——幂等增量，保留兜底。
- 已决策/有撤销历史的组：撤销语义优先，截图留在组里不动。
- `reject_screenshots` 瞬态组机制、`update_settings` 白名单（不新增设置项）。
- `jobs.py`：阶段名通过 `note` 透传，`stage_times` 自动记录，无需改动。

---

### Task 0: schema 基础——photos.is_screenshot 进 init_db（TDD）

**背景（必须先做）：** `is_screenshot` 列目前由 `screenshots._ensure_columns` 惰性 ALTER，`db.py` 基础 schema 没有。grouper/auto_resolve 的 SQL 一旦引用该列，任何尚未跑过截图分类的库（含所有测试 fixture 库）会 `no such column` 崩溃。仿照既有 `decisions.source` 的模式（DDL + `_ensure_column` 双保险，`db.py:62` + `db.py:141`），把列升为基础 schema——`main.py:71` 启动时 `init_db` 会迁移所有存量库。

**Files:**
- Modify: `backend/app/db.py:39-43`（photos DDL）与 `init_db`（约 db.py:141 后）
- Modify: `backend/tests/test_db.py`（新增 1 个测试）

- [ ] **Step 1: 写失败测试（test_db.py 追加）**

```python
def test_photos_have_is_screenshot_column(db):
    """基础 schema 即含 is_screenshot（grouper/auto_resolve 的 SQL 过滤依赖它，
    不等截图模块惰性 ALTER）。"""
    row = db.execute(
        "SELECT is_screenshot FROM photos LIMIT 1").fetchone()
    # 空表也能查询成功；列默认 NULL
    cols = {r["name"] for r in db.execute("PRAGMA table_info(photos)")}
    assert "is_screenshot" in cols
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_db.py -k is_screenshot -v`
Expected: FAILED（PRAGMA 无该列）。

- [ ] **Step 3: 修改 db.py**

photos DDL（db.py:39-43）old:
```sql
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    primary_file_id INTEGER REFERENCES files(id),
    group_id INTEGER REFERENCES groups(id)
);
```
new:
```sql
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    primary_file_id INTEGER REFERENCES files(id),
    group_id INTEGER REFERENCES groups(id),
    is_screenshot INTEGER   -- NULL=未分类, 1=截图(截图页处理), 0=照片
);
```
`init_db` 内，在 `_ensure_column(conn, "decisions", "source", ...)` 之后加一行：
```python
    _ensure_column(conn, "photos", "is_screenshot", "INTEGER")
```
（`screenshots._ensure_columns` 的重复 ALTER 被 `except OperationalError: pass` 吞掉，幂等共存；`SCHEMA_VERSION` 仿照 decisions.source 迁移先例不递增。）

- [ ] **Step 4: 验证**

Run: `.venv/bin/python -m pytest backend/tests/test_db.py -v`
Expected: 全部 PASSED。
Run: `.venv/bin/python -m pytest backend/tests/test_screenshots.py -v`
Expected: 全部 PASSED（证明与惰性 ALTER 无冲突）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/db.py backend/tests/test_db.py
git commit -m "feat: promote is_screenshot to base schema"
```

### Task 1: 相似分组排除截图（TDD）

**Files:**
- Modify: `backend/tests/test_grouper.py`（新增 2 个测试）
- Modify: `backend/app/grouper.py:276`

- [ ] **Step 1: 写失败测试（test_grouper.py 末尾追加）**

复用文件内既有 `ins_file` / `H0` / `AH0` / `DH0` / `T0` / `hex_at` 帮助函数（直接插行、手工填哈希，零图片解码）：

```python
def _mark_screenshot(db, photo_id):
    db.execute("UPDATE photos SET is_screenshot = 1 WHERE id = ?",
               (photo_id,))


def test_screenshot_flagged_photo_is_excluded_from_grouping(db):
    # 三张时间相邻、pHash 相同：两张普通照片 + 一张已标记截图
    pid_a, _ = ins_file(db, "a.jpg", H0, AH0, DH0, T0)
    pid_b, _ = ins_file(db, "b.jpg", H0, AH0, DH0, "2026-01-01T10:00:05")
    pid_s, _ = ins_file(db, "s.png", H0, AH0, DH0, "2026-01-01T10:00:10")
    _mark_screenshot(db, pid_s)
    report = grouper.build_groups(db)
    assert report["groups"] == 1
    rows = db.execute(
        "SELECT id, group_id FROM photos ORDER BY id").fetchall()
    by_id = {r["id"]: r["group_id"] for r in rows}
    assert by_id[pid_a] is not None and by_id[pid_a] == by_id[pid_b]
    assert by_id[pid_s] is None  # 截图不进组
    members = db.execute(
        "SELECT COUNT(*) FROM photos WHERE group_id IS NOT NULL").fetchone()[0]
    assert members == 2


def test_unclassified_null_is_screenshot_still_groups(db):
    # 兜底：is_screenshot 为 NULL（未分类）时按普通照片分组
    pid_a, _ = ins_file(db, "x.jpg", H0, AH0, DH0, T0)
    pid_b, _ = ins_file(db, "y.jpg", H0, AH0, DH0, "2026-01-01T10:00:05")
    db.execute("UPDATE photos SET is_screenshot = NULL")
    report = grouper.build_groups(db)
    assert report["groups"] == 1
    g = db.execute("SELECT group_id FROM photos WHERE id = ?", (pid_a,)).fetchone()
    assert g[0] is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_grouper.py -k screenshot -v`
Expected: `test_screenshot_flagged_photo_is_excluded_from_grouping` FAILED（截图进了组，members == 3）；`test_unclassified_null_is_screenshot_still_groups` PASSED（现状即兜底行为，作为回归锚点）。

- [ ] **Step 3: 修改候选 SQL（grouper.py:276）**

old:
```python
        " FROM files f JOIN photos p ON p.id = f.photo_id"
        " WHERE p.group_id IS NULL AND f.hash_phash IS NOT NULL"
        " AND f.hash_phash != ''"
```
new:
```python
        " FROM files f JOIN photos p ON p.id = f.photo_id"
        " WHERE p.group_id IS NULL AND f.hash_phash IS NOT NULL"
        " AND f.hash_phash != '' AND COALESCE(p.is_screenshot, 0) = 0"
```

（同函数上方注释块 270-271 行补一句：`# Known screenshots (is_screenshot=1) are handled on the screenshots page; NULL is treated as a photo (conservative fallback).`）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_grouper.py -v`
Expected: 全部 PASSED（含既有 11 个组逻辑测试，证明过滤不伤正常分组）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/grouper.py backend/tests/test_grouper.py
git commit -m "feat: exclude known screenshots from similarity grouping"
```

### Task 2: 自动去重排除截图（TDD）

**Files:**
- Modify: `backend/tests/test_auto_resolve.py`（新增 1 个测试）
- Modify: `backend/app/auto_resolve.py:70`

- [ ] **Step 1: 写失败测试（test_auto_resolve.py 追加）**

仿照该文件既有测试的照片/组构造帮助函数（手工插 pending 组 + 三哈希全零距离）；若文件内无帮助函数则参照 test_grouper.py 的 `ins_file` 风格现场构造：

```python
def test_identical_screenshots_are_not_auto_resolved(db):
    # pending 组内两张像素级相同(is_screenshot=1)的截图 → 不自动决策
    # 构造：插入组、两张截图照片、相同哈希，标记 is_screenshot=1
    # （具体插入代码按该文件既有 helper 风格写）
    ...
    report = auto_resolve.auto_resolve_pending(db)
    assert report["resolved_groups"] == 0
    # 组仍 pending、照片原地不动（group_id 不变、无 auto 决策行）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_auto_resolve.py -k "screenshot" -v`
Expected: FAILED（resolved_groups == 1，截图被自动决策）。

- [ ] **Step 3: 修改扫描 SQL（auto_resolve.py:70）**

old:
```sql
        WHERE g.status = 'pending' AND f.hash_phash IS NOT NULL
```
new:
```sql
        WHERE g.status = 'pending' AND f.hash_phash IS NOT NULL
        AND COALESCE(p.is_screenshot, 0) = 0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_auto_resolve.py -v`
Expected: 全部 PASSED。

- [ ] **Step 5: Commit**

```bash
git add backend/app/auto_resolve.py backend/tests/test_auto_resolve.py
git commit -m "feat: exclude known screenshots from auto-resolve"
```

### Task 3: ensure_analyzed 进度回调（TDD）

**Files:**
- Modify: `backend/tests/test_screenshots.py`（新增 1-2 个测试）
- Modify: `backend/app/screenshots.py:88-119`

- [ ] **Step 1: 写失败测试（test_screenshots.py 追加）**

```python
def test_ensure_analyzed_reports_progress(db, ...):
    # 复用该文件既有 _add_file 帮助函数插入 2-3 张未分类照片
    calls = []
    shots = screenshots_mod.ensure_analyzed(db, progress_cb=lambda *a: calls.append(a))
    assert shots["scanned"] >= 1
    assert all(c[0] == "screenshots" for c in calls)
    assert calls[0] == ("screenshots", 0, shots["scanned"])
    assert calls[-1][1] == calls[-1][2] == shots["scanned"]  # 结束于 total

def test_ensure_analyzed_empty_reports_zero_progress(db):
    calls = []
    screenshots_mod.ensure_analyzed(db, progress_cb=lambda *a: calls.append(a))
    assert calls == [("screenshots", 0, 0)]  # 空库也要发一次相位，UI 阶段列表依赖
```

（参数名按该文件既有 fixture 实际情况调整；`screenshots` 模块导入方式按文件头部既有 import 风格。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_screenshots.py -k progress -v`
Expected: FAILED（TypeError: unexpected keyword / 或 calls 为空）。

- [ ] **Step 3: 加 progress_cb 参数（screenshots.py:88）**

old:
```python
def ensure_analyzed(conn: sqlite3.Connection) -> dict:
    """Classify every photo not yet marked; idempotent per row."""
    _ensure_columns(conn)
    rows = conn.execute(
        "SELECT f.id AS fid, f.path, f.format, f.camera, p.id AS pid"
        " FROM photos p JOIN files f ON f.id = p.primary_file_id"
        " WHERE p.is_screenshot IS NULL"
    ).fetchall()
    if not rows:
        return {"scanned": 0, "found": 0}
    found = 0
    for r in rows:
```
new:
```python
def ensure_analyzed(conn: sqlite3.Connection, progress_cb=None) -> dict:
    """Classify every photo not yet marked; idempotent per row.

    ``progress_cb("screenshots", done, total)`` is called with
    ``done = 0`` before the loop starts and again after each row, so the
    pipeline's per-stage progress list sees the phase begin and finish.
    """
    _ensure_columns(conn)
    rows = conn.execute(
        "SELECT f.id AS fid, f.path, f.format, f.camera, p.id AS pid"
        " FROM photos p JOIN files f ON f.id = p.primary_file_id"
        " WHERE p.is_screenshot IS NULL"
    ).fetchall()
    total = len(rows)
    if progress_cb:
        progress_cb("screenshots", 0, total)
    if not rows:
        return {"scanned": 0, "found": 0}
    found = 0
    done = 0
    for r in rows:
```
循环体末尾（`found += 1` 之后、循环结束前）加：
```python
        done += 1
        if progress_cb:
            progress_cb("screenshots", done, total)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_screenshots.py -v`
Expected: 全部 PASSED。

- [ ] **Step 5: Commit**

```bash
git add backend/app/screenshots.py backend/tests/test_screenshots.py
git commit -m "feat: progress callback for screenshot classification"
```

### Task 4: 流水线插入截图识别阶段（TDD）

**Files:**
- Modify: `backend/tests/test_api.py:565-625`（更新 2 个既有测试 + 断言新键）
- Modify: `backend/app/api.py:596-598`

- [ ] **Step 1: 更新既有测试（先改断言，跑出失败）**

`test_run_full_pipeline_blocking`（test_api.py:568）：
- old: `assert set(report) == {"scan", "hashing", "grouping", "analysis", "recommend", "auto"}`
- new:
```python
    assert set(report) == {"scan", "hashing", "screenshots", "grouping",
                           "analysis", "recommend", "auto"}
    # 假图无法解码 → 尺寸未知 → 判为非截图（found=0），但被分类过（scanned=3）
    assert report["screenshots"] == {"scanned": 3, "found": 0}
```

`test_run_full_pipeline_blocking_reports_all_five_stages`（test_api.py:593）：
- phases 断言 old: `assert phases == {"scan", "hash", "group", "analysis", "recommend", "auto"}, phases`
- new: `assert phases == {"scan", "hash", "screenshots", "group", "analysis", "recommend", "auto"}, phases`
- 测试名与 docstring 更新为「all seven stages」。
- 注意循环内 `0 <= e[1] <= e[2]` 断言对 (0,0) 空相位成立；`entries[-1][1] == entries[-1][2]` 对 (0,0) 也成立。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_api.py -k "run_full_pipeline" -v`
Expected: 2 个 FAILED（缺 `screenshots` 键 / 缺 `screenshots` 相位）。

- [ ] **Step 3: 插入流水线步骤（api.py:596-598 之间）**

old:
```python
    hash_report = hashing.compute_hashes(conn, progress_cb=progress_cb)
    report["hashing"] = dict(hash_report)
    group_report = grouper.build_groups(conn, progress_cb=progress_cb)
```
new:
```python
    hash_report = hashing.compute_hashes(conn, progress_cb=progress_cb)
    report["hashing"] = dict(hash_report)
    # Classify screenshots BEFORE grouping so known screenshots stay out
    # of duplicate groups; NULL (unclassified) still groups (fallback).
    from app import screenshots as screenshots_mod
    shots_report = screenshots_mod.ensure_analyzed(
        conn, progress_cb=progress_cb)
    report["screenshots"] = dict(shots_report)
    group_report = grouper.build_groups(conn, progress_cb=progress_cb)
```

（函数 docstring 第一行同步更新为 `scan→hash→screenshots→group→analyze→recommend pipeline.`；`from app import` 就地导入与该文件 get_screenshots 的既有风格一致。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_api.py -v`
Expected: 全部 PASSED。

- [ ] **Step 5: Commit**

```bash
git add backend/app/api.py backend/tests/test_api.py
git commit -m "feat: classify screenshots in pipeline before grouping"
```

### Task 5: 前端进度条补「截图识别」阶段

**Files:**
- Modify: `frontend/dist/js/pages/settings.js:33-50`

- [ ] **Step 1: 修改 STAGE_LABELS 与 PIPELINE_STAGES**

STAGE_LABELS 在 `hash: '感知哈希',` 行后插入：`screenshots: '截图识别',`
PIPELINE_STAGES 在 `{ key: 'hash', label: '感知哈希' },` 行后插入：`  { key: 'screenshots', label: '截图识别' },`

（两处数组顺序即 UI 展示顺序；`PHASE_LABELS[job.note]` 回退机制保证未打标签的 note 也能显示，但 PIPELINE_STAGES 控制固定行列表，必须同步加。）

- [ ] **Step 2: 验证**

Run: `grep -n "screenshots" frontend/dist/js/pages/settings.js`
Expected: STAGE_LABELS 与 PIPELINE_STAGES 各 1 行命中（共 2 行）。
Run: `.venv/bin/python -m pytest backend/tests/test_main_routes.py -v`
Expected: 全部 PASSED（后端路由未动，防误伤）。

- [ ] **Step 3: Commit**

```bash
git add frontend/dist/js/pages/settings.js
git commit -m "feat: add screenshot stage to pipeline progress UI"
```

### Task 6: 全量验收与汇报

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/python -m pytest backend/tests -v`
Expected: 全部 PASSED（原 287 + 新增约 5 个）。

- [ ] **Step 2: 交叉验证 SQL 过滤落位**

Run: `grep -rn "COALESCE(p.is_screenshot" backend/app/`
Expected: 2 行命中——`grouper.py`、`auto_resolve.py` 各一。

- [ ] **Step 3: 汇报**

向用户汇报：改动清单、测试结果、验收标准逐条对照；提醒「已入库的老数据需重跑一次流水线，无决策历史的 pending 组会自动解散重建（截图出队），有撤销历史的组保留原样」；建议在 `http://127.0.0.1:8765` 设置页跑一次流水线，目检 7 阶段进度条与「截图识别」标签。
