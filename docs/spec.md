# Spec — Delta Log

> 本文档以 delta（变更条目）形式记录需求演进。最新变更在最上面，历史保持不动。
> 每条 delta 形如 `## [YYYY-MM-DD] <短标题>`，与 `testing.md` 同日期同标题的 delta 一一对应。
>
> 历史完整 spec 见 `docs/spec-YYYYMMDD.md`（按日期版本，最新一份是 `spec-20260429.md`，
> 包含到 §4.23 share 为止的全套需求）。本 delta-log 文件从 §4.24 起记录后续增量。

<!-- DELTA-INSERT-HERE -->

## [2026-04-30] 移动端 scrollback 浏览体验

**变更类型**: 新增
**动机**: 默认 xterm.js scrollback 1000 行，对长跑会话/`grep` 大输出不够用；手机上单指上下滑动经常被 xterm 的输入手势抢占，滚动起来卡顿；要回到很久之前的内容只能靠虚拟键盘的 PgUp 慢慢翻——触达成本高。把 A（扩 buffer）+ B（明确触摸方向）+ C（一键跳顶/跳底）一锅做掉。

### 4.25. 移动端 scrollback 浏览三连

**需求**:
- **A. 扩 scrollback**：xterm Terminal 初始化加 `scrollback: 10000`，可保留约 1 万行历史。
- **B. 明确触摸方向**：`.xterm-viewport` 加 `touch-action: pan-y` + `-webkit-overflow-scrolling: touch`，单指竖滑直接驱动 viewport 滚动，不再被 xterm 当作 terminal input 手势处理。
- **C. 一键跳顶 / 跳底**：虚拟键盘**第一行最右**追加两颗按钮：`⤒`（scroll-top）/ `⤓`（scroll-bot），点击调 `term.scrollToTop()` / `term.scrollToBottom()`，**仅本地滚动 xterm，不发送任何字节给 PTY**。

**约束**:
- 行 1 DOM 顺序：`⤒ ⤓ Ctrl Alt Esc ^B ^Bn Tab Home End ⌥⌫`（`⤒ ⤓` 在最左 = 最先被裁掉）；视觉**右对齐**（用 `.kp-row > :first-child { margin-left: auto }`——比 `justify-content: flex-end` 更可靠：后者在 overflow 时会把左侧 item 推进负空间且无法 scroll 过去，前者在 overflow 时 auto margin 折叠为 0，items 自然左排，scrollLeft 可达），右侧的 Ctrl…⌥⌫ 是右撇子拇指自然触达的位置，必须默认可见。
- 行 2 在 `PgDn` 之后插一颗 `{}` 键，一次点击发字面量 `{}`（用于 shell brace expansion，如 `mv file.{txt,bak}`）；不参与 sticky 修饰。
- **滚动初始位置贴右**：`.kp-row` 在加载 / `resize` / `orientationchange` 时由 JS 设 `scrollLeft = scrollWidth`，让右侧首先入眼；左侧 `⤒ ⤓` 在 360px 等窄屏默认看不到，用户向左 swipe 行内才能拉出。
- **键宽必须容纳标签**：`.kp` 改为 `flex: 0 0 auto`（不再平分宽度，按内容自然宽度），`min-width: 36px`（mobile 32px），`padding: 2px 6px`（mobile `3px 4px`，横向 padding 压缩让 360px 屏多容 1–2 颗键），`white-space: nowrap`——多字符标签 `Ctrl` / `PgUp` / `Home` / `^Bn` 不会被裁字；同时尽量让 `⌥⌫` 这类位置靠后的键在常见手机视宽内仍可见。
- 新按钮使用 `data-action` 属性（`scroll-top` / `scroll-bot`）而非 `data-key`，与现有 input-byte 派发路径解耦；`buildKey()` 不变。
- 不参与 sticky Ctrl/Alt（对 scroll 无意义）；不进入 §4.14 的 press-and-hold 连发逻辑（点一次到顶/到底已是终端动作）。
- 改动仅 `terminal.html`、`static/app.js`、`static/style.css` 三个文件；**无 .py 改动，无需重启服务**。
- **静态资源 cache-bust**（一次性 .py 改动）：`/` 路由渲染 `terminal.html` 时，把 `app.js` / `style.css` 的最大 `mtime` 作为 `static_version` 注入；模板的 `<script>` / `<link>` 加 `?v={{ static_version }}` 查询参数。文件改动 → mtime 变 → URL 变 → 浏览器强制拉新版，**避免本期问题（旧 JS 卡缓存导致 ⤒/⤓ 不生效）再现**。这一处需要重启一次 Python 服务装入新路由代码；之后所有静态资源改动都不再需要重启。
- **按键事件用 `pointerdown` 而非 `click`**：与 §4.14 既有 data-key 派发同构，iOS Safari 上 click 合成偶发不触发的问题不再出现。click 仅作 keyboard fallback（Space/Enter）。

**非目标**:
- 不在本期引 `@xterm/addon-search`（搜索 addon 是后续单独 spec）。
- 不调整 scrollback 上限的可配置性——10000 行是写死的合理默认。
- `touch-action: pan-y` 屏蔽了双指缩放手势在 viewport 区域的默认行为；如果用户依赖双指缩放看更小字号，请用浏览器层面的缩放手势（页面边缘）或字号设置。



**变更类型**: 新增
**动机**: tmux 用户需要 prefix `^B` 触手可及；移动端两行已经塞得很紧，再竖加一行会进一步压缩 xterm 视高。选 "选项 1：横向滚动当前两行（零设计成本）"——`.kp-row` 早已 `overflow-x: auto`，把第一行容量当作可横滑的 list 即可。

### 4.24. 虚拟键盘第一行新增 ^B / ^Bn，原 ^B 从第二行上移

**需求**: 第一行键序变为 `Ctrl Alt Esc ^B ^Bn Tab Home End ⌥⌫`；第二行去掉 `^B`，其余不变。`^Bn` 等价于按下 tmux prefix 后再按 `n`，对应 tmux **next-window**。
**约束**:
- `^B` 输出 `\x02`（已有键，仅位置改变）。
- `^Bn` 输出 `\x02n`（新增键码，由 `app.js` `buildKey()` 的 `ctrlbn` 分支处理；不参与 sticky Ctrl/Alt 修饰，因为它本身已是组合键）。
- `.kp-row` 已设 `overflow-x: auto` + `.kp` `min-width: 32px`，两行可装的键数随屏宽动态决定，超出时浏览器自动给该行加横向滚动条；本期不调整 CSS。
- 改动局限在 `terminal.html`（Jinja2 模板）与 `static/app.js`（静态 JS）两个文件，**不涉及任何 .py 改动**——starlette 的 Jinja2Templates 默认 `auto_reload=True`，下一次请求即生效；JS 由浏览器硬刷拿到。**无需 `systemctl restart wcmux`**。

**非目标**: 不做 Fn 模式 / 长按副值 / "更多" 抽屉等更复杂的多键容纳方案（属于后续讨论的选项 2–6，本期不做）；不调整键宽 / 键高 / 字号——若未来键多到挤不下再优化。
