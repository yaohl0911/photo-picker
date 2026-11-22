# 暗色模式设计

## 目标

浅色 / 深色 / 跟随系统三态主题；手动选择持久化 localStorage，默认跟随系统。

## 实现策略

- `frontend/dist/css/style.css`：
  - 全部颜色提取为 CSS 变量（背景/前景/面板/边框/主色/成功/警示/阴影/卡片底色等），`:root` 为浅色值
  - `[data-theme="dark"]` 覆盖为深色值
  - `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { …深色值… } }` 实现"跟随系统"（显式选 light 时强制浅色）
  - 浅色值必须与现状完全一致（浅色模式零视觉回归）；深色值注意文字对比度（正文 ≥ 4.5:1 量级）
  - 收尾自查：变量定义块之外不允许残留硬编码 `#hex` / `rgb(` 颜色
- `frontend/dist/index.html`：`<meta name="color-scheme" content="light dark">`
- `frontend/dist/js/app.js`：启动最早阶段读 localStorage `photo-picker.theme`（沿用 `photo-picker.*` 前缀先例）并设置 `document.documentElement.dataset.theme`；值为 `light`/`dark` 设置属性，`auto` 不设置属性，防 FOUC
- `frontend/dist/js/pages/settings.js`：主题三态选择控件（segmented control 或 select），切换即写 localStorage + 立即切换生效

## 验证

无前端自动化测试框架；由 lead 集成后在浏览器目检：三态切换、刷新持久化、浅色与现状无差异。

## 写域（agent-dark 独占，并行纪律）

`frontend/dist/css/style.css`, `frontend/dist/js/app.js`, `frontend/dist/js/pages/settings.js`, `frontend/dist/index.html`

其他 agent 正在并行修改不相交文件（backend/ 与 pages/compare.js、pages/overview.js、api.js、.github/、README.md）——绝不触碰上述写域之外的任何文件，不执行任何 git 写操作（commit/stash/checkout/reset 一律禁止，允许 git status/diff 只读）。
