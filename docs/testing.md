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



对应: spec.md `[2026-04-30] 移动端键盘与 scrollback 体验全面整顿` § E / 4.28

### 4.28. mdpreview / share 支持 PDF 与 LaTeX 源码文件

**PDF preview / share**

- [ ] 准备一个 `~/notes/sample.pdf`（≥ 1 页）。在 `/api/preview/list` 看到该文件，type=`pdf`
- [ ] `/api/preview/file?path=sample.pdf` 返回 JSON `{"type":"pdf","path":"sample.pdf"}`，**不**回 base64 / 不回 content
- [ ] preview 前端打开该文件，渲染为内嵌 PDF iframe（src 指向 `../../raw/preview?path=...`）；浏览器内置 PDF viewer 可翻页 / 缩放
- [ ] 对 sample.pdf 创建 share；查看 share 页是 `<iframe>` 内嵌 PDF，刷新仍可访问
- [ ] share 引用源文件（不复制字节）：删掉源文件 sample.pdf 后再访问 share 页，返回 410（与 image / markdown 删源行为一致）
- [ ] CSP 响应头含 `frame-src 'self'`，PDF iframe 不被浏览器阻塞

**LaTeX 源码 preview / share**

- [ ] 准备 `paper.tex` / `paper.bib` / `mystyle.cls` / `mypackage.sty` / `myrefs.bst` 各一份。`/api/preview/list` 看到全部 5 个，type=`code`
- [ ] preview 单独打开每个：返回 `{"type":"code","lang":"latex"|"bibtex","content":<src>}`；前端 hljs 把 `\section{}` `\cite{}` 等命令染色（不是单色 plaintext）
- [ ] `.tex` 里若含 `$E=mc^2$` 等数学块，**不**渲染为公式，按源码字符显示
- [ ] share `paper.tex` 后，分享页是高亮代码块；hljs CDN 失败时，降级为 plaintext `<pre>` 不报错
- [ ] `.bib` / `.bst` 在 share 页显示 bibtex 高亮（`@article{...}` 染色）
- [ ] 多文件 markdown 引用 `.tex` 时，`.tex` 不被误识别为图片资产；不进 `_collect_assets` 的 image 扫描

**中间产物排除**

- [ ] 准备 `paper.aux` / `paper.bbl` / `paper.toc` / `paper.log` 各一份。`/api/preview/list` 看不到这些（按 unknown 过滤）
- [ ] 直接 `/api/preview/file?path=paper.aux` 返回 4xx（type unknown，与既有 unknown 行为一致）
- [ ] 对 `paper.aux` create share 返回 `unsupported type for share`

**回归**

- [ ] markdown / image / html / drawio / jsonl / code / text 7 种既有类型 preview / share 行为未变（跑一遍各类型 happy path）
- [ ] `_RENDERABLE_TYPES` 不再拒绝 pdf；但仍拒绝 unknown / drawio / html / jsonl（这几个 share 不支持依然有效）



对应: spec.md `[2026-04-30] 移动端键盘与 scrollback 体验全面整顿` § E / 4.29

### 4.29. preview / share 代码块默认换行 + 显示行号

**默认换行**

- [ ] preview 打开任意 code 文件（如 `.py` 含一行 200 字符的注释），文本在视宽内换行，**不**横向滚动
- [ ] preview 打开含 fenced ``` ```python ``` 块的 markdown，长行同样换行
- [ ] share 一份 code 类型文件，远端打开后长行换行（不是横滚）
- [ ] share 一份含 fenced 代码块的 markdown，代码块内长行换行
- [ ] 含纯长 token（如 1000 字符的 base64）的代码行：换行保留 token 内字符不被强行断字（pre-wrap 在空白处断；token 无空白则触发 fallback 横滚），不会变成多行 garble
- [ ] `text` / `jsonl` 类型行为未变（既有 pre-wrap 仍生效）

**行号 — preview**

- [ ] 打开 code 文件，视图左侧出现行号列；行号从 1 开始连续
- [ ] DevTools 看 DOM 是 `<table class="hljs-ln">`，第 1 列 `td.hljs-ln-numbers`，第 2 列 `td.hljs-ln-code`
- [ ] 鼠标拖选**只能选中代码内容**，行号列不参与选区（`user-select: none` 生效）；复制粘贴拿到的纯文本里没有行号前缀
- [ ] markdown 内的 fenced code 块也带行号
- [ ] 切换浅色 / 深色主题，行号列颜色随之变（用 `--text2`），与代码区有 1px 分割线
- [ ] highlight.js CDN 行号插件 404 / 网络失败时，代码块仍能高亮显示（仅退化为无行号），不报 JS 异常

**行号 — share**

- [ ] share 一份 code 文件，远端 HTML 含 `<table class="highlighttable">`；左 `td.linenos`、右 `td.code`
- [ ] 行号列 `user-select: none`；选区复制不带行号
- [ ] 行号字体 tabular-nums，对齐 1/12/123 等不同位数
- [ ] share 一份含 fenced code 的 markdown：代码块带行号，但**普通 markdown 表格**（`| a | b |` 那种）仍被 `.md-table-scroll` 包裹（行号表不被误包）
- [ ] 浅色 / 深色主题切换：行号列颜色用 `--fg-faint`（淡色），代码区主色不变

**回归**

- [ ] 既有的 `.md table`（数据表格）依然有 `.md-table-scroll` 横向滚动包裹；shares.html / 既有 share 视图未破
- [ ] m13_share.py 整套测试仍 PASS（含 "table wrapped in md-table-scroll"）



对应: spec.md `[2026-04-30] 移动端键盘与 scrollback 体验全面整顿` § E / 4.30

### 4.30. share 行号与 wrap 后续行的对齐修正

- [ ] share 一份含长行（≥ 100 字符）的代码文件，**触发 wrap**：长行视觉换成 ≥ 2 行
- [ ] DOM 结构变了：share 渲染产物里**无** `<table class="highlighttable">`，改为 `<div class="highlight"><pre><span id="line-1">...</span><span id="line-2">...</span>...</pre>`
- [ ] 行号 `<span class="linenos">` 在每个 `id="line-N"` span 内最前；行号底色透明（不被 pygments 默认 stylesheet 染色）
- [ ] **wrap 触发后行号仍跟实际行对齐**：长行下一逻辑行的行号紧跟 wrap 续行结束位置，不再"漂"上去
- [ ] wrap 续行视觉缩进到行号右侧（与首字符对齐），不顶到行号正下方
- [ ] 选区复制（鼠标拖选代码区域 + Cmd+C）拿到的纯文本**不带行号**（`.linenos { user-select: none }`）
- [ ] 浅色 / 深色主题下行号颜色都正确（用 `--fg-faint`）
- [ ] m13_share.py 整套仍 PASS



对应: spec.md `[2026-04-30] 移动端键盘与 scrollback 体验全面整顿` § E / 4.31

### 4.31. mdpreview / share HTML 引用资源支持

**Preview HTML**

- [ ] 准备 `~/notes/page.html` 引用同目录 `figure.svg` / `style.css` / `clip.mp3` / `clip.mp4`（或类似媒体）
- [ ] 在 preview SPA 打开 `page.html`，iframe 加载，渲染出 `<h1>`、内嵌 SVG 显示、CSS 样式生效（`.boxed` 红边框）
- [ ] DevTools Network 面板看到 `/raw/preview/page.html` + 4 个 sibling 请求，**全部 200**（mp3/mp4 可以是 206 部分内容）
- [ ] iframe 里 `<a href="other.html">` 点击导航到 `/raw/preview/other.html`（同目录 sibling），不跳到顶层 SPA
- [ ] 含中文 / 空格的子目录（`my dir/中文.html`）也能加载，相对 ref 仍正确解析（`encodeURIComponent` per segment）

**Share HTML**

- [ ] 对同一 `page.html` create share，远端打开 share URL：iframe 渲染含图、含媒体的页面
- [ ] DevTools Network 看到 `/share/{date}/{seg}/raw/page.html` + 同目录 sibling 请求 200/206
- [ ] iframe 元素 attribute `sandbox="allow-scripts allow-popups allow-forms"`（允许脚本但 origin 是 null）
- [ ] iframe 内尝试 `document.cookie` 或 `localStorage` 不能读到 share 页面的 cookie / storage（origin null 隔离）

**Share 安全 scope**

- [ ] HTML 内构造 `<a href="../sibling/secret.txt">`：访问被 403（`relative_to(src_dir)` 拒绝跳出 src 父目录）
- [ ] HTML 内构造 `<img src="../../etc/passwd">`：访问被 403 / 404（traversal + 权限双拒）
- [ ] HTML 内构造 `<img src="../.env">`（隐藏文件）：访问 404（任何路径段以 `.` 开头一律拒绝）
- [ ] m13_share.py 既有 "unsupported type → 415" 仍生效（`data.bin` 仍是 unknown）
- [ ] **非 HTML share 不能借 tree 路由拿兄弟文件**：share 一份 `doc.pdf`，构造 `GET /share/.../raw/credentials.txt` 返回 **404**（`share_raw_tree` 因源 ftype != html 拒绝）；改回 `GET /share/.../raw?path=doc.pdf` 仍正常 200 拉源
- [ ] **markdown bundling 隐藏目录漏查修复**：md 文件含 `![](.hidden-dir/img.png)`，create share 后 `assets_skipped` 含此条目（reason 含 hidden），公开 share 不再 bundle 此图
- [ ] **create_share 拒绝隐藏文件源**：POST `/api/share` body 含 `{"path":".env"}` 返回 415（`cannot share hidden files`）；含 `{"path":".aws/credentials"}` 同 415；普通文件 200 不受影响

**X-Frame-Options + Content-Disposition 修正回归**

- [ ] **桌面 PDF share**：share 页 `<object>` 内嵌渲染 PDF；下方**不显示**"在新标签页打开 PDF"提示条（`@media (pointer: coarse)` 隐藏）—— 桌面零视觉新增
- [ ] **移动端 PDF share**：iOS Safari / Android Chrome 打开 share，`<object>` 区域可能空白/只显示首页（移动端固有限制），但下方提示条**显示**「移动端 / 嵌入查看不便？在新标签页打开 PDF」；点链接顶层跳转，调用系统 PDF viewer 正常渲染
- [ ] DevTools "Toggle device toolbar" 切到 iPhone/Pixel 模式，提示条变可见；切回 desktop 又隐藏（CSS-only，无 UA 嗅探）
- [ ] PDF share：DevTools 看 `/raw?path=...pdf` 响应头有：
  - `X-Frame-Options: SAMEORIGIN`
  - `Content-Security-Policy: frame-ancestors 'self'`
  - `Content-Disposition: inline; filename="<原文件名>"`
  - `Content-Type: application/pdf`
- [ ] 桌面 Chrome / Firefox / Safari / Edge：PDF inline 渲染（PDF viewer 工具栏可见，可翻页缩放）
- [ ] 移动 Safari iOS：PDF iframe 渲染（不下载） —— 之前没显式 `inline` 时移动 Safari 会触发下载提示
- [ ] PDF viewer 内"下载"按钮触发时，文件命名是源文件名（如 `report.pdf` 不是 `raw.pdf`）
- [ ] HTML share：iframe 正常加载（不出现 `ERR_BLOCKED_BY_RESPONSE`）
- [ ] Image share：iframe / `<img>` 嵌入仍正常
- [ ] drawio share：iframe 加载 embed.diagrams.net 与 share 主页面，不被 X-Frame-Options 阻拦
- [ ] 外站 `<iframe src="<我们的 share-url>">` 嵌入被浏览器拒绝（X-Frame-Options SAMEORIGIN + CSP frame-ancestors 'self' 双重防御）



对应: spec.md `[2026-04-30] 移动端键盘与 scrollback 体验全面整顿` § E / 4.32

### 4.32. share 支持 drawio

- [ ] 准备一个 `~/notes/diagram.drawio`（用 drawio 网页版导出 / 或手写 mxGraphModel XML）
- [ ] 对 diagram.drawio 创建 share；远端打开 share URL，加载几秒后看到嵌入的 drawio 图（不是源 XML 字符）
- [ ] iframe `src` 是 `https://embed.diagrams.net/?embed=1&proto=json&spin=1&dark=auto`
- [ ] DevTools Network：share 页面的 `<iframe>` 加载了 embed.diagrams.net 域；本域只发 share 主页面 `GET /share/{date}/{seg}` 一次（不需 raw 子请求 —— XML 直接嵌进了主页面 `<pre>`）
- [ ] DOM 中 `<pre id="drawio-xml" style="display:none">` 含 HTML-escape 后的原始 XML（看 textContent 应能解析为有效 XML）
- [ ] 缩放 / 平移 / 双击元素（drawio embed 自带）都正常工作
- [ ] **不出现** Save / Exit 按钮（`noSaveBtn:1, noExitBtn:1`）
- [ ] CSP header：`Content-Security-Policy` 含 `script-src 'self' 'unsafe-inline'` 和 `frame-src 'self' https://embed.diagrams.net`（drawio 类型独有；其它类型仍是 `script-src 'none'`）
- [ ] 含 `&` / `<` / `>` 的 XML（中文 / 实体 / `<mxCell>` 嵌套）round-trip 无损：drawio 渲染的图与源 XML 节点数 / 属性一致
- [ ] m13_share.py 整套仍 PASS（drawio 加进 `_RENDERABLE_TYPES` 但 "unsupported type → 415" 测试用的是 `data.bin` / unknown，不冲突）
