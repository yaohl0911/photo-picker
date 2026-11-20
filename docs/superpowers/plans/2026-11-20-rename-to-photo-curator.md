# 「照片精选助手」改名 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把产品显示名从「照片去重助手」改为「照片精选助手」，共 5 处纯文本替换，内部标识符不动。

**Architecture:** 纯文案替换、零逻辑改动。改动集中在 `README.md`（标题 + 定位句）与 `frontend/dist/`（`index.html` 的 title 标签与页头品牌名、`app.js` 的 `document.title` 模板）。验收靠全仓 grep（旧名零残留、新名就位）与既有 pytest 套件（证明无回归）。设计依据：`docs/superpowers/specs/2026-11-20-rename-to-photo-curator-design.md`。

**Tech Stack:** 无新依赖；bash `grep` 验证 + 既有 pytest。

**刻意不碰（重要）：**
- 仓库名 `photo-picker`、淘汰目录 `_photo_picker_rejected`、localStorage 前缀 `photo-picker.*`、页脚 `photo-picker · 本地运行`——稳定内部标识符。
- `.harmony-build/`（HarmonyOS 打包暂存，gitignored、生成物）：不手改；下次重新打包时会从 `frontend/dist` 取新名。完成后提醒用户重新打包即可。
- `.playwright-cli/`（测试会话快照，gitignored）：历史记录，不碰。
- `docs/superpowers/`（spec/plan 文档）：旧名作为历史记录保留。

---

### Task 1: README 标题与定位句

**Files:**
- Modify: `README.md:1`
- Modify: `README.md:3`

- [ ] **Step 1: 记录基线**

Run: `grep -n "照片去重" README.md`
Expected: 2 行命中——第 1 行标题 `# 照片去重助手`、第 3 行定位句含「照片去重与偏好学习工具」。

- [ ] **Step 2: 改标题（README.md:1）**

old: `# 照片去重助手`
new: `# 照片精选助手`

- [ ] **Step 3: 改定位句（README.md:3）**

old: `本地优先的照片去重与偏好学习工具：扫描相册目录，用感知哈希找出相似照片组，基于质量分析给出「保留哪张」的推荐和理由，你在并排对比中做决策，每一次决策都会反哺偏好权重，越用越懂你。`

new: `本地优先的照片精选与偏好学习工具：扫描相册目录，用感知哈希找出相似照片组、识别积攒的截图，基于质量分析给出「保留哪张」的推荐和理由，你在并排对比中做决策，每一次决策都会反哺偏好权重，越用越懂你。`

（唯一差异：`照片去重` → `照片精选`，并在「相似照片组」后补「、识别积攒的截图」；句中其余文字逐字保留。）

- [ ] **Step 4: 验证**

Run: `grep -n "照片去重" README.md`
Expected: 无输出（空）。
Run: `grep -n "照片精选" README.md`
Expected: 第 1、3 行命中。

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: rebrand as photo curator in README"
```

### Task 2: 前端显示名（index.html + app.js）

**Files:**
- Modify: `frontend/dist/index.html:8`
- Modify: `frontend/dist/index.html:20`
- Modify: `frontend/dist/js/app.js:75`

- [ ] **Step 1: 记录基线**

Run: `grep -rn "照片去重助手" frontend/`
Expected: 3 行命中——`frontend/dist/index.html:8`、`frontend/dist/index.html:20`、`frontend/dist/js/app.js:75`。

- [ ] **Step 2: 改浏览器标签标题（index.html:8）**

old: `  <title>照片去重助手</title>`
new: `  <title>照片精选助手</title>`

- [ ] **Step 3: 改页头品牌名（index.html:20）**

old: `        <span class="brand__name">照片去重助手</span>`
new: `        <span class="brand__name">照片精选助手</span>`

- [ ] **Step 4: 改动态 document.title（app.js:75）**

old: "  document.title = `照片去重助手 · ${ROUTES[name].label}`;"
new: "  document.title = `照片精选助手 · ${ROUTES[name].label}`;"

（整行替换，模板字符串与缩进原样保留，只换 4 个字。）

- [ ] **Step 5: 验证**

Run: `grep -rn "照片去重助手" frontend/`
Expected: 无输出（空）。
Run: `grep -rn "照片精选助手" frontend/`
Expected: 3 行命中（同 Step 1 的三个位置）。

- [ ] **Step 6: Commit**

```bash
git add frontend/dist/index.html frontend/dist/js/app.js
git commit -m "feat: rebrand web UI as photo curator"
```

### Task 3: 全仓验收

**Files:** 无改动（纯验证 + 汇报）。

- [ ] **Step 1: 旧名零残留**

Run: `grep -rn "照片去重助手" . --exclude-dir=.git --exclude-dir=.harmony-build --exclude-dir=.playwright-cli --exclude-dir=superpowers`
Expected: 无输出（空）。排除项均为历史记录或生成物：git 历史、HarmonyOS 打包暂存、playwright 快照、superpowers 文档。

- [ ] **Step 2: 新名就位 5 处**

Run: `grep -rn "照片精选" README.md frontend/dist/index.html frontend/dist/js/app.js`
Expected: 5 行命中——`README.md:1`、`README.md:3`（定位句「照片精选与偏好学习工具」，不含"助手"后缀）、`frontend/dist/index.html:8`、`frontend/dist/index.html:20`、`frontend/dist/js/app.js:75`。

- [ ] **Step 3: 既有测试全过（无回归证明）**

Run: `.venv/bin/python -m pytest backend/tests -v`
Expected: 全部 PASS。改名不触后端，理论上零影响；DSH 沙箱内若遇 fastapi/pydantic 导入错误属环境约束（见 `docs/DEVENV.md`），改在用户终端复跑确认。

- [ ] **Step 4: 汇报与提醒**

向用户汇报：4 处旧名已清零、新名 5 处就位、测试结果；提醒两点——(a) 若要重新打包 HarmonyOS 版，重新构建以让 `.harmony-build` 内的 webui 副本拿到新名；(b) 页面标题效果可开 `http://127.0.0.1:8765` 目视确认（标签页 + 页头品牌名 + 切换页面后的动态标题）。
