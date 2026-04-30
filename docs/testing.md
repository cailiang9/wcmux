# Testing Checklist — Delta Log

> 本文档以 delta（变更条目）形式记录验证清单的演进。最新变更在最上面，历史保持不动。
> 每条 delta 对应 `spec.md` 同日期同标题的 delta。
>
> 历史完整 testing checklist 见 `docs/testing-YYYYMMDD.md`（最新一份 `testing-20260429.md` 含 §4.23 全套）。本 delta-log 文件从 §4.24 起记录后续增量。

<!-- DELTA-INSERT-HERE -->

## [2026-04-30] 虚拟键盘横向滚动 + 新增 ^B 与 ^Bn

对应: spec.md `[2026-04-30] 虚拟键盘横向滚动 + 新增 ^B 与 ^Bn`

### 4.24. 虚拟键盘第一行新增 ^B / ^Bn

- [ ] 浏览器硬刷后，虚拟键盘第一行可见 `Ctrl Alt Esc ^B ^Bn Tab Home End ⌥⌫`，第二行不再含 `^B`
- [ ] 在 tmux 会话里 tap `^B` 后再 tap `c`，能新建窗口（验证移位后的 `^B` 仍是 prefix `\x02`）
- [ ] 在 tmux 会话里 tap `^Bn`（一次点击），自动切到下一个 tmux window（等价于按 prefix 后 n）
- [ ] sticky Ctrl/Alt 状态在 `^B` / `^Bn` 上**不**叠加（这两键本身已是组合，再叠 Ctrl 会破坏字符）
- [ ] 在窄屏（≤ 360px）下若一行键累积宽度超出可视宽度，行内可横向滑动；不挤占第二行 / 不挤占终端区域
- [ ] **不重启 Python 服务**仅改 `terminal.html` 与 `static/app.js` 后，浏览器硬刷即看到新键并能正常工作（Jinja2 auto_reload + static file 浏览器缓存破除）
- [ ] 不破坏 §4.16 既有 `^B` 行为；不破坏 §4.12 sticky 修饰对其他键的作用
