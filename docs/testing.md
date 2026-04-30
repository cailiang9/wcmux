# Testing Checklist — Delta Log

> 本文档以 delta（变更条目）形式记录验证清单的演进。最新变更在最上面，历史保持不动。
> 每条 delta 对应 `spec.md` 同日期同标题的 delta。
>
> 历史完整 testing checklist 见 `docs/testing-YYYYMMDD.md`（最新一份 `testing-20260429.md` 含 §4.23 全套）。本 delta-log 文件从 §4.24 起记录后续增量。

<!-- DELTA-INSERT-HERE -->

## [2026-04-30] 移动端 scrollback 浏览体验

对应: spec.md `[2026-04-30] 移动端 scrollback 浏览体验`

### 4.25. 移动端 scrollback 浏览三连

- [ ] 在新打开的 tab 里跑 `for i in $(seq 1 5000); do echo line $i; done`，全部 5000 行可滚回（验证 scrollback ≥ 5000；若设了 10000 应至少能看 5000 行往上）
- [ ] DevTools 的 Application → Local Storage 不需要看；通过 `term.options.scrollback` 在 console 应能读到 10000（或当前配置值）
- [ ] 手机浏览器（iOS Safari + Android Chrome 各一次）单指**竖向滑动**xterm 区域，scrollback 应顺滑滚动，不再"卡一下" / 不被吞
- [ ] 触摸滑动期间不触发 xterm 的选区高亮 / 文本选择菜单
- [ ] 虚拟键盘整体**右对齐**；行 1 默认可见的最右一颗键是 `⌥⌫`（不是 `⤓`），右撇子拇指自然落点上是 `Ctrl Alt Esc ^B …` 这些常用键
- [ ] 行 1 在 360px 窄屏上默认**看不到** `⤒` 和 `⤓`（它们在左侧 overflow 区域，需要向左 swipe 行内才能拉出）；拉出后点击仍然工作（顶/底滚动）
- [ ] 旋转手机或调整浏览器窗口宽度后，行 1 仍贴右显示（`scrollLeft` 自动重新到 `scrollWidth`，不会停在中间状态）
- [ ] 行 2 在 PgDn 之后看到 `{}` 键；点击后终端收到字面量 `{}`（DevTools Network → WS 帧 `{"type":"input","data":"{}"}`）；与 sticky Ctrl/Alt 不互动
- [ ] **每颗按键完整显示标签**——多字符 `Ctrl` / `Alt` / `PgUp` / `PgDn` / `Home` / `^Bn` / `⌥⌫` 不被裁字、不省略号；窄屏（≤ 360px）下若一行键累积宽度超出可视宽度，行内可横向滑动而不是压缩字号
- [ ] 点 `⤒` / `⤓` 之后，**没有**任何字节被发到 PTY（DevTools Network → WS 帧无 `{"type":"input"...}` 出现）
- [ ] sticky Ctrl 或 Alt 处于激活状态时点 `⤒` / `⤓`，sticky 状态保持（不被这两键消耗），且滚动仍正常
- [ ] 多 tab 场景下，点 `⤒` / `⤓` 只影响**当前激活 tab**的 xterm，不影响别的 tab
- [ ] iOS Safari 的双指缩放在 xterm viewport 内被 `touch-action: pan-y` 抑制；在页面其它区域（如顶栏）仍可缩放——验证手势隔离不过界
- [ ] 改动后**仅浏览器硬刷**（不重启 wcmux 服务）即可看到新键和新行为
- [ ] DevTools → Elements 看 `<script src=".../app.js?v=...">`，`v` 参数是某个 epoch 数字；把 `app.js` 触一下 `touch`，刷新页面后 `v` 变化，浏览器实际请求新 URL（Network 面板可见 200，不是 304）
- [ ] iOS Safari 上点 `⤒` / `⤓` **首次** tap 即触发滚动（pointerdown 路径不再依赖 click 合成）；多次 tap 之间无延迟



对应: spec.md `[2026-04-30] 虚拟键盘横向滚动 + 新增 ^B 与 ^Bn`

### 4.24. 虚拟键盘第一行新增 ^B / ^Bn

- [ ] 浏览器硬刷后，虚拟键盘第一行可见 `Ctrl Alt Esc ^B ^Bn Tab Home End ⌥⌫`，第二行不再含 `^B`
- [ ] 在 tmux 会话里 tap `^B` 后再 tap `c`，能新建窗口（验证移位后的 `^B` 仍是 prefix `\x02`）
- [ ] 在 tmux 会话里 tap `^Bn`（一次点击），自动切到下一个 tmux window（等价于按 prefix 后 n）
- [ ] sticky Ctrl/Alt 状态在 `^B` / `^Bn` 上**不**叠加（这两键本身已是组合，再叠 Ctrl 会破坏字符）
- [ ] 在窄屏（≤ 360px）下若一行键累积宽度超出可视宽度，行内可横向滑动；不挤占第二行 / 不挤占终端区域
- [ ] **不重启 Python 服务**仅改 `terminal.html` 与 `static/app.js` 后，浏览器硬刷即看到新键并能正常工作（Jinja2 auto_reload + static file 浏览器缓存破除）
- [ ] 不破坏 §4.16 既有 `^B` 行为；不破坏 §4.12 sticky 修饰对其他键的作用
