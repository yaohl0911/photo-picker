# 收藏与评分（正向精选信号）设计

## 背景与目标

当前所有用户交互都是"减法"（去重淘汰、截图清理、偏好学习），缺少"这张我喜欢"的正向信号。本功能为后续智能相册（评分≥N / 仅收藏规则）与精选导出提供地基。

两个独立信号，各占一列：

- `is_favorite`（布尔，默认 0）：❤️ 收藏
- `rating`（0-5 星，NULL=未评分）

## Schema 变更

`backend/app/db.py` photos 表 DDL 增加两列（新库）：

```sql
rating        INTEGER,               -- NULL=未评分, 0-5
is_favorite   INTEGER NOT NULL DEFAULT 0,  -- 0/1
```

`init_db()` 用 `_ensure_column` 懒迁移存量库（先例：photos.is_screenshot）：

- `_ensure_column(conn, "photos", "rating", "INTEGER")`
- `_ensure_column(conn, "photos", "is_favorite", "INTEGER NOT NULL DEFAULT 0")`
- 注意：SQLite ALTER ADD COLUMN 带 NOT NULL 必须给 DEFAULT——此处已带，先例模式可直接复用。

## API 契约

`PATCH /api/photos/{photo_id}`，JSON body 至少含一项：

- `"rating"`：`0|1|2|3|4|5|null`（null = 清除评分；必须严格整数，`"3"` 这类字符串拒绝）
- `"is_favorite"`：`true|false`

| 情况 | 返回 |
|---|---|
| 校验失败（超界/类型错/无有效字段/空 body） | 422（跟随 api.py 现有错误返回风格） |
| photo_id 不存在 | 404 |
| 成功 | 200 `{"id": <photo_id>, "rating": <int|null>, "is_favorite": <bool>}` |

## 前端交互

- `frontend/dist/js/api.js` 新增 `updatePhoto(photoId, patch)`（跟随现有 fetch 封装风格）
- `frontend/dist/js/pages/compare.js` 每张照片的信息面板：
  - ❤️ 收藏切换：点击立即 PATCH，乐观更新，失败回滚 + toast
  - ★ 1-5 评分条：点击第 N 星 = 评分 N；再次点击当前分数 = 清除（null）；hover 高亮预览
- `frontend/dist/js/pages/overview.js` 组卡片缩略图：收藏照片显示角标 ❤️（不显示星，控制卡片信息密度）

## 不做（明确出界）

- 智能相册规则、时间线、导出（Wave 2 范围）
- 按评分排序 / 批量评分
- 决策流程联动：淘汰与收藏互不影响（被淘汰照片的 photos 行保留，收藏状态自然存续）

## 测试（TDD，先红后绿）

- `backend/tests/test_db.py`：新库 photos 表含 rating / is_favorite 列
- `backend/tests/test_api.py`：
  - PATCH `{"is_favorite": true}` → 200 且生效
  - PATCH `{"rating": 3}` → 200；再 PATCH `{"rating": null}` → 清除回 NULL
  - PATCH `{"rating": 6}` / `{"rating": "3"}` → 422
  - PATCH 不存在 id → 404
  - 空 body / 无有效字段 → 422

## 写域（agent-a1 独占，并行纪律）

`backend/app/db.py`, `backend/app/api.py`, `backend/app/main.py`（路由注册：HTTP 路由层是 main.py thin adapter，服务函数 `update_photo(conn, photo_id, patch)` 在 api.py，异常映射 ValueError→422 / KeyError→404）, `backend/tests/test_db.py`, `backend/tests/test_api.py`, `backend/tests/test_main_routes.py`, `frontend/dist/js/api.js`, `frontend/dist/js/pages/compare.js`, `frontend/dist/js/pages/overview.js`

其他 agent 正在并行修改不相交文件（style.css / app.js / settings.js / .github/ / README.md）——绝不触碰上述写域之外的任何文件，不执行任何 git 写操作（commit/stash/checkout/reset 一律禁止，允许 git status/diff 只读）。
