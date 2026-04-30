# Spec — Delta Log

> 本文档以 delta（变更条目）形式记录需求演进。最新变更在最上面，历史保持不动。
> 每条 delta 形如 `## [YYYY-MM-DD] <短标题>`，与 `testing.md` 同日期同标题的 delta 一一对应。
>
> 历史完整 spec 见 `docs/spec-YYYYMMDD.md`（按日期版本，最新一份是 `spec-20260429.md`，
> 包含到 §4.23 share 为止的全套需求）。本 delta-log 文件从 §4.24 起记录后续增量。

<!-- DELTA-INSERT-HERE -->

## [2026-04-30] 虚拟键盘横向滚动 + 新增 ^B 与 ^Bn

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
