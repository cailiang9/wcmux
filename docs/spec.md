# Spec — Delta Log

> 本文档以 delta（变更条目）形式记录需求演进。最新变更在最上面，历史保持不动。
> 每天**一个** `## [YYYY-MM-DD] <当日主题汇总>` 顶级 delta；当天有多条变更时，
> 在其下按"需求类型"分组（三级标题），每个独立需求保持自己的 `### §4.N` 小节，
> 不在同一 §的 `**约束**` 列表里混塞多个无关关注点。
>
> 历史完整 spec 见 `docs/spec-YYYYMMDD.md`（按日期版本，最新一份是 `spec-20260429.md`，
> 包含到 §4.23 share 为止的全套需求）。本 delta-log 文件从 §4.24 起记录后续增量。

<!-- DELTA-INSERT-HERE -->

## [2026-04-30] 移动端键盘与 scrollback 体验全面整顿

> 当日 4 组需求：A 终端历史浏览（§4.25）；B 虚拟键盘扩展键（§4.24）；
> C 虚拟键盘排版与命中（§4.26）；D 静态资源版本（§4.27）。

---

### A. 终端历史浏览

#### 4.25. 移动端 scrollback 浏览三连

**变更类型**: 新增
**动机**: 默认 xterm.js scrollback 1000 行对长跑会话/`grep` 大输出不够用；手机上单指上下滑动经常被 xterm 的输入手势抢占；要回到很久之前内容只能靠 PgUp 慢慢翻——触达成本高。

**需求**:
- **A. 扩 scrollback**：xterm Terminal 初始化加 `scrollback: 10000`，可保留约 1 万行历史。
- **B. 明确触摸方向**：`.xterm-viewport` 加 `touch-action: pan-y` + `-webkit-overflow-scrolling: touch`，单指竖滑直接驱动 viewport 滚动，不再被 xterm 当作 terminal input 手势处理。
- **C. 一键跳顶 / 跳底**：虚拟键盘第一行追加 `⤒`（scroll-top）/ `⤓`（scroll-bot）两颗按钮，点击调 `term.scrollToTop()` / `term.scrollToBottom()`，**仅本地滚动 xterm，不发送任何字节给 PTY**。

**约束**:
- 新按钮使用 `data-action` 属性（`scroll-top` / `scroll-bot`）而非 `data-key`，与现有 input-byte 派发路径解耦；`buildKey()` 不变。
- 不参与 sticky Ctrl/Alt（对 scroll 无意义）；不进入 §4.14 的 press-and-hold 连发逻辑（点一次到顶/到底已是终端动作）。

**非目标**:
- 不在本期引 `@xterm/addon-search`（搜索 addon 是后续单独 spec）。
- 不调整 scrollback 上限的可配置性——10000 行是写死的合理默认。
- `touch-action: pan-y` 屏蔽双指缩放手势在 viewport 区域的默认行为；如需更小字号请用浏览器层面缩放。

---

### B. 虚拟键盘扩展键

#### 4.24. 第一行新增 ^B / ^Bn，原 ^B 从第二行上移；第二行新增 `{}`

**变更类型**: 新增
**动机**: tmux 用户需要 prefix `^B` 触手可及；shell brace expansion (`mv file.{txt,bak}`) 在移动键盘上敲 `{` `}` 极不顺手——做成单键直发字面量。

**需求**:
- 第一行键序变为 `Ctrl Alt Esc ^B ^Bn Tab Home End ⌥⌫`；第二行去掉 `^B`。
- `^Bn` 等价于按下 tmux prefix 后再按 `n`（next-window）。
- 第二行 `PgDn` 之后新增 `{}` 键，单击发字面量 `{}`。

**约束**:
- `^B` 输出 `\x02`（已有键，仅位置改变）。
- `^Bn` 输出 `\x02n`（新增键码，由 `app.js` `buildKey()` 的 `ctrlbn` 分支处理）。
- `^B` / `^Bn` / `{}` 均不参与 sticky Ctrl/Alt 修饰（前两者本身已是组合，后者是字面量）。
- 改动局限在 `terminal.html` 与 `static/app.js` 两个文件，**无 .py 改动**——Jinja2 `auto_reload=True`，浏览器硬刷即生效；**无需重启服务**。

**非目标**: 不做 Fn 模式 / 长按副值 / "更多" 抽屉等更复杂的多键容纳方案（属选项 2–6，本期不做）。

---

### C. 虚拟键盘排版与命中

#### 4.26. 行 1 右对齐 + 键宽容标 + pointerdown 派发

**变更类型**: 新增
**动机**: 多键累积后窄屏（≤ 360px）一行装不下；需把右撇子拇指的自然落点（Ctrl…⌥⌫）保留为默认可见，把次要键（⤒⤓）放到左侧 overflow 区。同时 iOS Safari 上 click 合成偶发不触发 → 改 `pointerdown`。

**需求**:
- 行 1 DOM 顺序：`⤒ ⤓ Ctrl Alt Esc ^B ^Bn Tab Home End ⌥⌫`（`⤒ ⤓` 在最左 = 最先被裁掉）；视觉**右对齐**。
- 滚动初始位置贴右：`.kp-row` 在加载 / `resize` / `orientationchange` 时由 JS 设 `scrollLeft = scrollWidth`。
- 每颗按键完整显示标签，多字符 `Ctrl` / `PgUp` / `^Bn` / `⌥⌫` 不被裁字。
- 按键事件用 `pointerdown` 而非 `click`；click 仅作 keyboard fallback（Space/Enter）。

**约束**:
- 右对齐用 `.kp-row > :first-child { margin-left: auto }`——比 `justify-content: flex-end` 更可靠：后者在 overflow 时会把左侧 item 推进负空间且无法 scroll 过去，前者在 overflow 时 auto margin 折叠为 0，items 自然左排，scrollLeft 可达。
- `.kp` 改为 `flex: 0 0 auto`（按内容自然宽度），`min-width: 36px`（mobile 32px），`padding: 2px 6px`（mobile `3px 4px`），`white-space: nowrap`。
- 与 §4.14 既有 data-key 派发同构。
- 改动仅 `static/app.js`、`static/style.css`、`terminal.html` 三个文件，**无 .py 改动**。

**非目标**: 不调整字号；不引 CSS Grid 重写键盘骨架。

---

### D. 静态资源版本

#### 4.27. 静态资源 cache-busting（mtime → ?v=）

**变更类型**: 新增
**动机**: 本期遇到旧 JS 卡浏览器缓存导致 `⤒/⤓` 不生效；以后所有静态资源改动都希望浏览器自动拿最新版，不依赖用户硬刷。

**需求**:
- `/` 路由渲染 `terminal.html` 时，把 `app.js` / `style.css` 的最大 `mtime` 作为 `static_version` 注入模板上下文。
- 模板的 `<script src=".../app.js">` / `<link href=".../style.css">` 加 `?v={{ static_version }}` 查询参数。
- 文件改动 → mtime 变 → URL 变 → 浏览器强制拉新版。

**约束**:
- 这是**唯一**需要重启 Python 服务的改动（装入新路由代码）；之后所有静态资源改动都不再需要重启。
- `static_version` 取整数 epoch 即可，不需要 hash；mtime 粒度（秒）足够区分人工编辑节奏。

**非目标**: 不做基于内容 hash 的指纹（content-hash），mtime 已经够用且无需读文件内容。

---

### E. 文件预览/分享扩展类型

> 与 A–D 的键盘 / scrollback / 缓存破除主题独立；属当日另一条需求线，按 memory 「同日按需求类型分组」原则并入今日 delta。

#### 4.28. mdpreview / share 支持 PDF 与 LaTeX 源码文件

**变更类型**: 新增
**动机**: 当前 `file_type()` 只认 image / html / drawio / jsonl / markdown / code / text；PDF（论文、合同、扫描件）和 LaTeX 源码（`.tex` / `.bib` / `.cls` / `.sty` / `.bst`）在终端工作流里很常见，落进 `unknown` 桶就既不能预览也不能分享。本期一次性纳入。

**需求**:
- **PDF**：`.pdf` 归类为新 file type `"pdf"`。
  - preview：`/api/preview/file?path=…` 对 pdf 不返回正文，返回 `{"type":"pdf","path":"<path>"}`；前端用 `<iframe src="../../raw/preview?path=…">` 直显，依赖浏览器内置 PDF viewer。
  - share：`_RENDERABLE_TYPES` 加入 `"pdf"`；分享视图复用既有 image 类型的引用模式——通过 `/share/{date}/{seg}/raw?path=<source_url_path>` 端点直读源文件字节，**不**复制到 share 资产目录；生成的静态页面是 `<iframe src=<share-base>/raw?path=…>`。源文件被删/移走 → share_view 已经会返回 410（与 image / markdown 同机制）。
- **LaTeX 源码（带语法高亮）**：以下后缀加入 `CODE_EXTS`，走现有 `code` 路径并配 `LANG_MAP` 让 highlight.js 上色：
  - `.tex` → `latex`（LaTeX 文档源码）
  - `.cls` → `latex`（文档类源码）
  - `.sty` → `latex`（宏包源码）
  - `.bib` → `bibtex`（BibTeX 文献库）
  - `.bst` → `bibtex`（BibTeX 样式源码）
  - 高亮在前端由 highlight.js 完成（preview / share 已内置 hljs 加载），后端只回 `{"type":"code","lang":"latex"|"bibtex","content":<src>}`。

**约束**:
- PDF 走"引用"路径，**不**进入 `_scan_assets_for_share` 的 image 扫描（pdf 类型在该函数里直接返回空 assets，与现有 image 类型同处置）；故无新增 asset 大小检查、无新增字节配额逻辑、无新增清理逻辑。
- 浏览器内置 PDF viewer 跳转控件（缩放、翻页、下载）由浏览器自行控制；服务端不干预。
- **移动端 PDF 嵌入降级**（修订）：`<iframe>` / `<object>` 在 iOS Safari、Android Chrome 等移动端浏览器内无法可靠渲染 PDF（多数情况显示空白或仅首页），但**顶层导航**到 PDF URL 时移动浏览器一律调用系统原生 viewer（iOS QuickLook / Android 内置 PDF）正常渲染。share 视图改用 `<object>` + 永久 fallback 链接的两条腿方案：桌面继续 inline 渲染（`<object>` 与 `<iframe>` 行为相同），移动端 `<object>` 失败时下方"在新标签页打开 PDF"链接顶层跳转触发原生 viewer。fallback 链接用 `@media (pointer: coarse)` 仅在触屏设备显示，桌面**完全不可见、零视觉变化**。
- **CSP 必须含 `object-src 'self'`**：`<object>` 受 `object-src` 指令管，**不**走 `frame-src`；原 `_CSP_STRICT` 仅设了 `frame-src 'self'`，`object-src` 落到 `default-src 'none'` 兜底被拒，导致桌面浏览器全员显示 `<object>` 内层 fallback 文案"此浏览器无法内嵌渲染 PDF"。修法：`_CSP_STRICT` 增加 `object-src 'self';`，覆盖 PDF 嵌入路径。
- LaTeX 5 个后缀走 `code` 路径不需任何后端逻辑改动，只动 `CODE_EXTS` 与 `LANG_MAP` 两个常量集合即可生效；preview / share 的 code 分支不变。
- 前端需确认 highlight.js 包含 `latex` 与 `bibtex` 两个 language（默认 CDN 全量包已含；若用 common 子集需显式按需加载）。preview 和 share 模板**任一**缺这两个 language 时，应优雅降级为 `plaintext` 高亮而非报错。
- `file_type()` 返回 `"pdf"` 不影响既有调用方：`_RENDERABLE_TYPES` 显式枚举，新类型主动加入；其它分支（如 search 索引）按"非 unknown 一律放行"的策略已自动覆盖。
- 改动文件：`src/wcmux/preview.py`（新增 `PDF_EXTS` 与 `file_type` 的 pdf 分支、`CODE_EXTS` 增 5 个后缀、`LANG_MAP` 增 5 个映射、`/api/preview/file` 的 pdf 分支）、`src/wcmux/share_routes.py`（`_RENDERABLE_TYPES` 加 pdf、`_render_share_body` 加 pdf iframe 分支、CSP 加 `frame-src 'self'`、内嵌 CSS 加 `.pdf-wrap`）、`src/wcmux/static/preview/preview.html`（hljs 加载 latex/bibtex language module、renderFile 加 pdf 分支）。

**非目标**:
- **不**编译 TeX → PDF / HTML：不引 `tectonic` / `latexmk` / `pdflatex`，TeX 文件仅作高亮源码呈现。需要排版结果的用户自己跑编译再分享生成的 PDF。
- **不**做客户端 KaTeX / MathJax 数学公式渲染（即使 `.tex` 里有 `$…$`，也按源码字符显示，不渲染公式）。
- **不**做 PDF 全文搜索 / 缩略图 / 文字提取——服务端不解析 PDF 内容，仅做字节中转。
- **不**纳入 LaTeX 中间产物：`.aux` / `.bbl` / `.toc` / `.lof` / `.lot` / `.idx` / `.log` / `.fls` / `.fdb_latexmk` / `.synctex.gz` 等编译副产品按 `unknown` 处理（不可预览不可分享），避免 share 列表被脏文件淹没。`.dtx` / `.ins` / `.ltx` 等不常见 LaTeX 扩展暂不纳入，后续按需补。

#### 4.29. preview / share 代码块默认换行 + 显示行号

**变更类型**: 新增
**动机**: 长行（含长 URL / 单行 minified JSON / shell 长 pipeline）在原 `overflow-x: auto` 下被横向裁掉，移动端尤其难读；定位讨论时口头报"第几行"也需要实际看到行号。

**需求**:
- **默认换行**：所有 preview 与 share 的代码 / markdown fenced code 块默认 `white-space: pre-wrap; word-break: break-word`；保留 `overflow-x: auto` 作为单一超长 token（base64 块、二进制 hash 等）的 fallback。`text` / `jsonl` 既有 `pre-wrap`，无变化。
- **行号**：preview 与 share 的 code（含 markdown fenced code）显示左侧行号列：
  - **preview**：引入 `highlightjs-line-numbers.js` (CDN)；hljs 高亮后调 `hljs.lineNumbersBlock(block)` 在 `<pre><code>` 外包成两列 `<table class="hljs-ln">`，左 `td.hljs-ln-numbers` 右 `td.hljs-ln-code`。
  - **share**：服务端 pygments `HtmlFormatter(linenos="table")`，输出 `<table class="highlighttable">`；`_wrap_tables` 显式跳过 `highlighttable` 类，避免叠 `.md-table-scroll`（line-number 表不需要横向滚动包装）。

**约束**:
- 行号列**禁止可选中**（`user-select: none`），以保证选区复制只拿到代码本身、不带行号。
- 行号样式独立：右对齐、`font-variant-numeric: tabular-nums`、淡色（用 `--text2` / `--fg-faint`），与代码区之间细分割线。
- 换行规则同时作用于 markdown 内 fenced code（`.md pre`）与单文件 code 视图（`.code-wrap pre` / `.highlight pre`）；`pre code` 显式继承 `white-space` / `word-break` 防止内层覆盖外层。
- 改动仅 CSS / 模板 / pygments formatter 参数；**preview 全在静态资源层**（HTML + CSS），浏览器硬刷生效；**share 需重启**一次 Python 服务（pygments formatter 在 `_ensure_pygments` 里 lazy 创建，进程内常驻）。
- 文件改动：`src/wcmux/static/preview/preview.html`（CDN 加载行号插件、hljs 后调 `lineNumbersBlock`）、`src/wcmux/static/preview/style.css`（加 `.hljs-ln*` 规则、`.md-body pre` / `.code-wrap pre` 加 wrap）、`src/wcmux/share_routes.py`（pygments `linenos="table"`、内嵌 CSS 加 `.highlighttable*` 与 `.md pre` wrap、`_wrap_tables` 跳 `highlighttable`）。

**非目标**:
- **不**做点击行号高亮 / 选中区间的"行号 anchor 链接"（如 GitHub 的 `#L12-L20`）。
- **不**做行号-列号 (1:23) 的双重定位；只显示行号。
- **不**给 `text` / `jsonl` 视图加行号——`text` 是普通文本，`jsonl` 已有自己的 line index。本期只改 code 路径。
- **不**做长 token 主动断字（仅 fallback 横滚），避免破坏粘贴回去的语义（如硬断 base64 还原失败）。

#### 4.30. share 行号与 wrap 后续行的对齐修正（行号架构改写）

**变更类型**: 修改
**Supersedes**: `[2026-04-30] §4.29` 中 share 用 `linenos="table"` 的实现选型。
**动机**: §4.29 用 pygments `linenos="table"` 把行号 / 代码分两个 `<pre>` 渲染、靠 `vertical-align: top` 对齐——一旦任何一行因 `pre-wrap` 换成多视觉行，行号 pre 仍是每号一行，从此往下整列错位（"line 5" 飘到 line 4 实际位置）。这是 `linenos="table"` 的固有限制。

**需求**:
- pygments formatter 改用 `linenos="inline" + linespans="line"`：每个源代码行被 pygments 包成 `<span id="line-N"><span class="linenos">N</span>code</span>`，行号天然附在自己那行内。
- CSS 改写：
  - `.highlight pre > span[id^="line-"]`：`display: block` + `padding-left: 4em` + `text-indent: -4em` —— 让首字符（行号）落在 0 处，wrap 续行缩进到 4em（行号右侧），视觉对齐保持。
  - `.highlight .linenos`：`inline-block`、`width: 3em`、右对齐、`user-select: none`、淡色 + 右分割线。
- 删除 §4.29 留下的所有 `.highlighttable*` 规则与 `_wrap_tables` 对 `highlighttable` 的特判。

**约束**:
- pygments `linespans="line"` 的输出格式是 `<span id="line-N">`（id 不是 class），CSS 选择器必须用 `[id^="line-"]`。
- pygments 内置的 `.linenos` class 在 pygments 自己的 stylesheet 里也有规则（padding-left/right 5px、background），需用 `background: transparent !important` 覆盖避免双底色。
- 改动文件：`src/wcmux/share_routes.py`（`HtmlFormatter` 参数 + 内嵌 CSS）；preview 端不变（preview 用 highlight.js 行号插件，每行一个 `<tr>` 天生对齐，无此问题）。

**非目标**:
- 不改 preview 实现（已对齐，无需改写）；本期只修 share。
- 不引入 line anchor / hover highlight 等 GitHub-style 增强（仍属 §4.29 非目标范畴）。

#### 4.31. mdpreview / share HTML 引用资源（图/音/视/CSS/JS）支持

**变更类型**: 新增
**动机**: HTML 文件常引用同目录或子目录的 `<img>` / `<video>` / `<audio>` / `<source>` / `<link rel=stylesheet>` / `<script src>` 等。原 preview iframe 用 `src="/raw/preview?path=foo.html"` 加载，HTML 内 `<img src="x.png">` 浏览器解析为 `/raw/x.png?...`（query string 被替换），404；share 端则 html 不在 `_RENDERABLE_TYPES`，直接 415。本期让 HTML 真正能渲染含媒体的页面。

**需求**:
- **新增路径式 raw 路由**：
  - preview：`GET /raw/preview/{path:path}`（query-param 形式 `/raw/preview?path=…` 保留，仅用于支持绝对路径下的 extra root）。
  - share：`GET /share/{date}/{seg}/raw/{path:path}` —— 任何 HTML iframe 的相对资源引用都命中此路由。
- **HTML iframe 用路径式 URL**：preview / share 两端 `<iframe src>` 都用路径式（每段 `encodeURIComponent`、`/` 保留），让 iframe 文档的 base URL 携带目录层级，相对引用自然解析为同目录 sibling。
- **share 把 html 加进 `_RENDERABLE_TYPES`**：可分享，分享视图就是一个内嵌 iframe + sandbox 限制。
- **媒体后缀识别**：`MEDIA_EXTS = {.mp4, .webm, .mov, .avi, .mkv, .m4v, .mp3, .wav, .ogg, .oga, .m4a, .flac}` 与 `FONT_EXTS = {.woff, .woff2, .ttf, .otf, .eot}` 加进 `file_type()`，分别返回 `"media"` / `"font"`，使 raw 路由不再因 `unknown` 拒绝它们。这两个新类型**不**进 `_RENDERABLE_TYPES`（不可作为 share 主体），仅作 HTML 子资源被引用。

**约束**:
- **share scope 限制**：`share_raw_tree` 只允许目标解析后**位于源 HTML 的父目录或其子目录**（`relative_to(src_dir)`）。源文件本身、同目录 sibling、子目录文件都允许；源父目录之外（包括 `..` traversal、绝对路径跨 root、扩展根下的兄弟等）一律 403。隐藏文件（任何路径段以 `.` 开头）一律 404，避免通过 ref 泄露 `.env` / `.git/...` 等敏感文件。
- **`share_raw_tree` 仅对 HTML 类型放开**：`source_real_path` 经 `file_type()` 判定后必须为 `"html"`，否则 404。这是关键防御 —— 否则任何 share URL（pdf / image / markdown）的持有者都能构造 `/raw/credentials.json` 拿同目录兄弟，违反"一个 share 一个文件"的承诺。pdf / image / markdown 等类型的 raw 全走 `share_raw_source` 严校验路径（`path == source_url_path`）。
- **markdown asset bundling 的 hidden 检查必须查全路径段**：`_scan_assets_for_share` 在判定一个 markdown 内嵌图是否能 bundle 时，原仅查 `real.name.startswith(".")`，会漏掉 `![](.hidden-dir/photo.png)` 这种隐藏目录下的非隐藏文件名情况。修法对齐 `share_raw_tree` 的 `any(part.startswith(".") for part in real.parts)`。
- **`create_share` 主动拒绝隐藏文件源**：`path` 解析后若任何路径段以 `.` 开头，415 拒创建。这条挡的是用户**意外**操作（path 直接打 `.env` / `.bash_history` / `.aws/credentials` 这种），创建者本来就有 auth 能拉这些文件，但避免一个手抖把 share URL 复制粘贴到 Slack。listing / search 已默认过滤 dotfile，share 创建路径之前是直通的 path 参数。
- **iframe sandbox**：share HTML iframe 使用 `sandbox="allow-scripts allow-popups allow-forms"` —— 允许脚本运行（HTML 页面交互），但 origin 是 null（不可访问 share 页面 storage / cookie），杜绝跨页污染。
- **CSP 区分**：
  - share 主页（HTML 类）：`_CSP_HTML` —— `script-src 'none'`（外页本身没有脚本）+ `frame-src 'self'`（允许 iframe 加载本域）。
  - share raw 路由（asset / source / tree 三处）：用 `_SHARE_RAW_HEADERS`，CSP 只设 `frame-ancestors 'self'`（同源 share 页面可以 iframe 嵌入它，外站不行）；同时 **`X-Frame-Options` 必须从基础 `DENY` 改为 `SAMEORIGIN`**——旧浏览器忽略 CSP `frame-ancestors`，仅看 X-Frame-Options，DENY 会导致 iframe 加载被拒，PDF 因此 fallback 成"直接下载"对话框。**修正了原 `frame-ancestors 'none'` + `X-Frame-Options: DENY` 把 share 页面自己的 iframe 也拒掉的 bug**——这个 bug 同时影响 §4.28 PDF iframe 和 §4.32 drawio iframe。
  - 同时 raw 路由显式设 `Content-Disposition: inline; filename="..."`——明确告诉浏览器"展示而不是下载"（默认无 Content-Disposition 时，桌面 Chrome/Firefox 已会内联渲染 PDF，但移动 Safari、某些代理 / 安全套件、嵌入式浏览器在 application/pdf 无 Content-Disposition 时倾向于下载）。`filename` 取源文件名，用户在 PDF viewer 里点"下载"会拿到合理的命名。
- **绝对路径源的降级**：若 source_url_path 以 `/` 开头（来自 extra root），路径式 URL 无法承载，回落到 query-param 形式 `<share-base>/raw?path=...`；HTML 本身可显示，但其内部相对引用解析失败（属已知降级，本期不解决）。
- **改动文件**：`src/wcmux/preview.py`（`MEDIA_EXTS` / `FONT_EXTS` + `file_type` 分支 + `raw_file_tree` 路由）、`src/wcmux/static/preview/preview.html`（HTML iframe 改路径式 URL）、`src/wcmux/share_routes.py`（`_RENDERABLE_TYPES` 加 html、`_CSP_HTML`、`_SHARE_RAW_HEADERS` 替换 `_SHARE_HTTP_HEADERS` 用法、`share_raw_tree` 路由、`_render_share_body` 加 html 分支、`.html-wrap` 内嵌 CSS）。

**非目标**:
- **不**对 HTML 做服务端解析 / 资源 bundling / URL 重写（不像 markdown 图片那种 asset 列表）。引用按浏览器原生解析，谁错谁负责。
- **不**支持跨 preview root 的引用（HTML 在 root A、引用 root B 的资源会 403），由 scope 限制保证 share 隔离。
- **不**在分享出去的 HTML 里禁用脚本（已用 sandbox 隔离危害；用户共享 HTML 时自己负责内容安全）。
- **不**做 HTML 内 anchor `#xxx` 的滚动联动（iframe 自包含）。

#### 4.32. share 支持 drawio（embed.diagrams.net iframe + postMessage 注入 XML）

**变更类型**: 新增
**动机**: §4.22 preview 已通过 `embed.diagrams.net` iframe + postMessage 完整支持 drawio 编辑；share 一直把 drawio 拒在 `_RENDERABLE_TYPES` 之外（415），用户分享一张图给同事只能截图或导出 PNG 再 share image。本期让接受者直接看到可缩放、可平移的真 drawio 图，不需源文件。

**需求**:
- `_RENDERABLE_TYPES` 加入 `"drawio"`。
- share 视图渲染：把 `.drawio` 的 XML 文本读出来，HTML-escape 后塞进一个隐藏 `<pre id="drawio-xml" style="display:none">…</pre>`；同页 `<iframe src="https://embed.diagrams.net/?embed=1&proto=json&spin=1&dark=auto">`；一段内联 `<script>` 监听 `message` 事件，在 iframe 发出 `init` 时通过 `postMessage` 把 `{action:"load", xml, autosave:0, saveAndExit:0, noSaveBtn:1, noExitBtn:1}` 推给 iframe，drawio 渲染图。
- XML 通过 `<pre>.textContent` 读回（HTML-escape 自动反转）—— 不引入 fetch，不打 XMLHttpRequest，不依赖 `connect-src`。

**约束**:
- **CSP 单独放宽**（仅 drawio 类型）：`_CSP_DRAWIO` = `default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-src 'self' https://embed.diagrams.net; frame-ancestors 'none';` —— 比其它类型多两项：`script-src` 加 `'unsafe-inline'`（postMessage 那段内联 JS），`frame-src` 白名单 drawio 嵌入域。
- 受 §4.31 同款 `X-Frame-Options` bug 影响（drawio iframe 也是 same-origin parent → external child），但 drawio 的 child 是 `embed.diagrams.net` 由 drawio 自己的服务端控制响应头，不走我们的 raw 路由，所以本身**不**受 X-Frame-Options 影响。隐含的反向：drawio 的 share 页面被 X-Frame-Options DENY 限制了"share 页面被外站嵌入"，符合预期。
- XML escape 的 round-trip 必须无损：`html.escape(xml)` 处理 `<`/`>`/`&`/`"`/`'`，`pre.textContent` 读回时浏览器自动 unescape，得到原始 XML 字节。这覆盖 drawio 文件中常见的 `<mxCell>` 等标签和 `&amp;` 等实体。
- postMessage 收发严格校验 `e.origin === "https://embed.diagrams.net"`，避免恶意子帧伪造 init 事件触发 load。
- 改动文件：`src/wcmux/share_routes.py`（`_RENDERABLE_TYPES` 加 drawio、`_CSP_DRAWIO`、`_share_headers` 分支、`_render_share_body` 加 drawio 分支、`.drawio-wrap` 内嵌 CSS）。

**非目标**:
- **不**让接受者编辑保存（drawio embed 协议提供 `noSaveBtn:1, noExitBtn:1` 让 UI 不显示这些按钮；底层的 save 事件即使触发也无后端写入路径——share 路由没暴露 save endpoint）。
- **不**做 server-side 渲染 SVG/PNG（避免引 drawio CLI 的 ~500MB electron 依赖）；接受者的浏览器要能访问 `embed.diagrams.net` 域。
- **不**为 drawio share 单独引入 asset bundling（`<image href>` 等内嵌资源走 drawio embed 自己的 fetch 机制，与本期 share scope 解耦）。
