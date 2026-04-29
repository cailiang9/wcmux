"""Share routes — authed CRUD under /api/share and public render under /share.

Public routes:
  GET  /share/{date}/{slug_id}        # rendered HTML for the shared file
  GET  /share/{date}/{slug_id}/a      # raw bytes for an inlined asset (?path=)

Authed routes (Depends(require_auth)):
  POST    /api/share                  # create
  GET     /api/share                  # list
  DELETE  /api/share/{share_id}       # revoke

The public routes do NOT call require_auth — the random 12-char id IS the
credential, and the LockoutRegistry that gates /login also gates lookups
here so 4xx-then-guess attacks are rate-limited at the IP level.
"""
from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path
from typing import Optional

import markdown
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .auth import LockoutRegistry, _client_ip, require_auth
from .preview import (
    CODE_EXTS, IMAGE_EXTS, JSONL_EXTS, LANG_MAP, MARKDOWN_EXTS, TEXT_EXTS,
    file_type, format_size, format_time, resolve_path,
)
from .shares import (
    EXPIRY_PRESETS, MAX_ASSETS, MAX_ASSETS_BYTES, ShareRegistry,
    expiry_seconds, iter_markdown_image_urls, parse_id_from_segment,
    rewrite_markdown_images, slugify,
)

router = APIRouter()

_RENDERABLE_TYPES = {"markdown", "code", "text", "image"}

_SHARE_HTTP_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Frame-Options": "DENY",
    # Locks down rendered share HTML: no inline scripts, no external assets,
    # only same-origin <img> (we serve them from /share/.../a) plus inline
    # styles for syntax-highlighting / pygments noclasses output.
    "Content-Security-Policy":
        "default-src 'none'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'none'; "
        "frame-ancestors 'none';",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


# ---- create / list / revoke (authed) ----

@router.post("/api/share")
async def create_share(request: Request, _=Depends(require_auth)) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    body = body or {}
    src_path = body.get("path", "")
    if not src_path or not isinstance(src_path, str):
        raise HTTPException(status_code=400, detail="missing path")
    expires_label = body.get("expires_in", "1y")
    if not isinstance(expires_label, str) or expires_label not in EXPIRY_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"expires_in must be one of {sorted(EXPIRY_PRESETS)}")
    label = body.get("label", "") or ""

    roots = request.app.state.preview_roots
    target = resolve_path(roots, src_path)

    # Both unresolved AND resolved must lie under some root (stricter than
    # /api/preview/file: spec §4.23 wants share scope tighter than preview).
    if not _strict_under_roots(target, roots):
        raise HTTPException(
            status_code=400,
            detail="source resolves (via symlink?) outside preview roots")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")

    ftype = file_type(target)
    if ftype not in _RENDERABLE_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported type for share: {ftype} (markdown/code/text/image only in MVP)")

    # Asset bundle: only relevant for markdown.
    assets, assets_skipped = _scan_assets_for_share(target, ftype, roots)

    slug_input = target.stem or target.name or "share"
    slug = slugify(slug_input)
    secs = expiry_seconds(expires_label)
    assert secs is not None  # validated above

    registry: ShareRegistry = request.app.state.shares
    entry = registry.create(
        source_url_path=src_path,
        source_real_path=str(target),
        slug=slug,
        label=label,
        expires_in=secs,
        assets=assets,
        assets_skipped=assets_skipped,
    )
    url = _share_url(request, entry)
    return JSONResponse({
        "id": entry["id"],
        "url": url,
        "date": entry["date"],
        "slug": entry["slug"],
        "expires_at": entry["expires_at"],
        "expires_in": expires_label,
        "assets_included": [a["url_path"] for a in entry["assets"]],
        "assets_skipped": entry["assets_skipped"],
    })


@router.get("/api/share")
def list_shares(request: Request, _=Depends(require_auth)) -> JSONResponse:
    registry: ShareRegistry = request.app.state.shares
    rows = []
    for s in registry.list():
        rows.append({
            "id": s["id"],
            "date": s["date"],
            "slug": s["slug"],
            "label": s.get("label", ""),
            "source_url_path": s["source_url_path"],
            "created_at": s["created_at"],
            "expires_at": s["expires_at"],
            "view_count": s.get("view_count", 0),
            "last_viewed": s.get("last_viewed", 0),
            "url": _share_url(request, s),
        })
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return JSONResponse({"shares": rows})


@router.delete("/api/share/{share_id}")
def revoke_share(share_id: str, request: Request,
                 _=Depends(require_auth)) -> JSONResponse:
    registry: ShareRegistry = request.app.state.shares
    if not registry.revoke(share_id):
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse({"ok": True})


# ---- public render + asset ----

@router.get("/share/{date}/{seg}")
def share_view(date: str, seg: str, request: Request) -> HTMLResponse:
    sid, share = _lookup_or_4xx(date, seg, request)
    src = Path(share["source_real_path"])
    # Source may have moved / been deleted / been on a now-unmounted drive
    # since the share was created; render a friendly 410 instead of a 500
    # that leaks tracebacks into journald and confuses the recipient.
    if not src.exists() or not src.is_file():
        return _share_error_html(410, "Shared file is no longer available")
    ftype = file_type(src)
    if ftype not in _RENDERABLE_TYPES:
        return _share_error_html(415, "Unsupported file type for share")

    # Update view count BEFORE rendering so a render-time error still records
    # the access (helps spot abuse).
    request.app.state.shares.touch(sid)
    try:
        body_html = _render_share_body(share, src, ftype, request)
    except OSError as e:
        # mid-read EIO / permission flips / dir suddenly unmounted, etc.
        return _share_error_html(410, f"Shared file is unreadable: {e}")
    page = _wrap_share_page(share, body_html, src, ftype)
    return HTMLResponse(page, headers=_SHARE_HTTP_HEADERS)


@router.get("/share/{date}/{seg}/a")
def share_asset(date: str, seg: str, request: Request,
                path: str = Query(...)) -> FileResponse:
    sid, share = _lookup_or_4xx(date, seg, request)
    asset = next((a for a in share.get("assets", []) if a["url_path"] == path),
                 None)
    if not asset:
        raise HTTPException(status_code=404)
    real = Path(asset["real_path"])
    if not real.exists() or not real.is_file():
        raise HTTPException(status_code=404)
    headers = dict(_SHARE_HTTP_HEADERS)
    return FileResponse(real, headers=headers)


# ---- helpers ----

def _strict_under_roots(p: Path, roots: list[Path]) -> bool:
    """spec §4.23: share scope demands BOTH unresolved-under-root AND
    resolved-under-root. Defends against symlinks pointing outside."""
    try:
        real = p.resolve(strict=False)
    except OSError:
        return False
    for r in roots:
        try:
            real.relative_to(r.resolve())
            return True
        except ValueError:
            continue
    return False


def _lookup_or_4xx(date: str, seg: str, request: Request) -> tuple[str, dict]:
    """Resolve URL segments to a share entry, applying lockout/expiry rules.
    Raises HTTPException for any failure path. Date is purely cosmetic — the
    id alone is the credential — but if it doesn't match the entry's stored
    date we 404 (mismatched URL likely a stale paste)."""
    lockout: LockoutRegistry = request.app.state.lockout
    ip = _client_ip(request)
    if lockout.is_locked(ip):
        raise HTTPException(status_code=429, detail="too many attempts")
    sid = parse_id_from_segment(seg)
    if not sid:
        lockout.record_failure(ip)
        raise HTTPException(status_code=404)
    registry: ShareRegistry = request.app.state.shares
    share = registry.get(sid)
    if not share or share.get("date") != date:
        lockout.record_failure(ip)
        raise HTTPException(status_code=404)
    exp = int(share.get("expires_at", 0))
    if exp != 0 and time.time() > exp:
        # Don't lock out for expired — the link was once valid, the recipient
        # isn't necessarily attacking. Just say so.
        raise HTTPException(status_code=410, detail="share expired")
    lockout.record_success(ip)
    return sid, share


def _share_url(request: Request, entry: dict) -> str:
    """Build the public share URL. Uses the configured base_url so reverse-
    proxy deployments produce correct links to hand out."""
    base = request.app.state.config.base_url.rstrip("/")
    return f"{base}/share/{entry['date']}/{entry['slug']}-{entry['id']}"


def _scan_assets_for_share(target: Path, ftype: str, roots: list[Path]):
    """Return (assets, assets_skipped). Only markdown contributes assets;
    other file types share with `assets=[]`. For the source file itself, we
    also need to be reachable as an asset (its url_path is the original
    source_url_path), but for now we don't add it to the bundle: rendering
    happens server-side and reads `source_real_path` directly."""
    if ftype != "markdown":
        return [], []
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [], [{"path": "", "reason": f"unreadable source: {e}"}]

    source_dir = target.parent
    assets: list[dict] = []
    skipped: list[dict] = []
    seen_real: set[str] = set()
    total_bytes = 0

    for raw_url in iter_markdown_image_urls(text):
        if len(assets) >= MAX_ASSETS:
            skipped.append({"path": raw_url, "reason": "asset count cap reached"})
            continue
        # Resolve raw_url → candidate FS path
        if raw_url.startswith("/"):
            candidate = Path(raw_url)
        else:
            candidate = source_dir / raw_url

        # Both unresolved + resolved must fall under some root.
        if not _strict_under_roots(candidate, roots):
            skipped.append({"path": raw_url, "reason": "outside preview roots"})
            continue
        try:
            real = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            skipped.append({"path": raw_url, "reason": "cannot resolve"})
            continue
        if not real.is_file():
            skipped.append({"path": raw_url, "reason": "not a regular file"})
            continue
        if real.name.startswith("."):
            skipped.append({"path": raw_url, "reason": "hidden file"})
            continue
        if real.suffix.lower() not in IMAGE_EXTS:
            skipped.append({"path": raw_url, "reason": "not an image extension"})
            continue
        try:
            size = real.stat().st_size
        except OSError:
            skipped.append({"path": raw_url, "reason": "stat failed"})
            continue
        if total_bytes + size > MAX_ASSETS_BYTES:
            skipped.append({"path": raw_url, "reason": "asset bytes cap reached"})
            continue
        if str(real) in seen_real:
            # Already bundled (different relative URL pointing same file).
            continue
        seen_real.add(str(real))
        total_bytes += size
        # url_path is what /share/.../a?path=... receives. We use the original
        # raw_url unchanged so markdown rewriting just points <img src=raw_url>
        # at the asset endpoint with that same string — round-trip preserved.
        assets.append({
            "url_path": raw_url,
            "real_path": str(real),
            "size": size,
        })
    return assets, skipped


def _render_share_body(share: dict, src: Path, ftype: str,
                       request: Request) -> str:
    """File-type dispatch → HTML body fragment (NOT a full document)."""
    if ftype == "markdown":
        text = src.read_text(encoding="utf-8", errors="replace")
        # Rewrite local <img> URLs to go through /share/.../a?path=<orig>.
        bundled = {a["url_path"] for a in share.get("assets", [])}
        share_base = _share_path(share)

        def _replace(url: str) -> Optional[str]:
            if url not in bundled:
                return None
            from urllib.parse import quote
            return f"{share_base}/a?path={quote(url, safe='')}"

        rewritten = rewrite_markdown_images(text, _replace)
        rendered = markdown.markdown(
            rewritten,
            extensions=["fenced_code", "tables", "toc", "nl2br",
                        "sane_lists", "attr_list"],
        )
        # Code fences inside markdown: pygments highlight.
        rendered = _highlight_code_blocks(rendered)
        # spec §4.23: wrap each <table> so it scrolls horizontally on its
        # own without forcing the whole page to scroll on narrow viewports.
        rendered = _wrap_tables(rendered)
        return f'<div class="md">{rendered}</div>'

    if ftype == "code":
        text = src.read_text(encoding="utf-8", errors="replace")
        lang = LANG_MAP.get(src.suffix.lower(), "text")
        return _pygments_highlight(text, lang)

    if ftype == "text":
        text = src.read_text(encoding="utf-8", errors="replace")
        return f'<pre class="text">{html.escape(text)}</pre>'

    if ftype == "image":
        from urllib.parse import quote
        share_base = _share_path(share)
        url = share["source_url_path"]
        # The source file itself isn't in `assets` (assets only collects
        # secondary inline images for markdown). For an image-typed share
        # we serve the source via the asset endpoint by adding a sentinel
        # entry on access — simplest is to include it in assets at create-
        # time, but we keep create-time logic narrow. Instead, for image
        # shares, render <img> pointed at /share/.../raw which we add below.
        return (f'<div class="img-wrap">'
                f'<img alt="" src="{html.escape(share_base)}/raw?path='
                f'{quote(url, safe="")}"></div>')

    # Should not reach (filtered earlier).
    return f"<p>Unsupported type: {html.escape(ftype)}</p>"


def _share_path(share: dict) -> str:
    """Relative path component used inside rendered HTML for asset URLs.
    Page is at '/share/<date>/<slug-id>'; '/a' and '/raw' are siblings."""
    return f"{share['slug']}-{share['id']}"


@router.get("/share/{date}/{seg}/raw")
def share_raw_source(date: str, seg: str, request: Request,
                     path: str = Query(...)) -> FileResponse:
    """Public alias to fetch the share's source file (image-typed shares).
    `path` MUST equal the share's source_url_path; otherwise 404."""
    sid, share = _lookup_or_4xx(date, seg, request)
    if path != share["source_url_path"]:
        raise HTTPException(status_code=404)
    real = Path(share["source_real_path"])
    if not real.exists() or not real.is_file():
        raise HTTPException(status_code=404)
    headers = dict(_SHARE_HTTP_HEADERS)
    return FileResponse(real, headers=headers)


def _wrap_share_page(share: dict, body_html: str, src: Path, ftype: str) -> str:
    title = html.escape(src.name)
    label = html.escape(share.get("label", "") or "")
    expires = (format_time(int(share["expires_at"]))
               if share.get("expires_at") else "永不过期")
    label_block = f'<span class="meta-label">「{label}」</span>' if label else ""
    css = _share_page_css()
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · share</title>
<style>{css}</style>
</head>
<body>
<header class="share-hd">
  <span class="filename">{title}</span>
  {label_block}
  <span class="meta-exp">过期: {html.escape(expires)}</span>
</header>
<main class="share-body">{body_html}</main>
<footer class="share-ft">wcmux share · spec §4.23 · type={html.escape(ftype)}</footer>
</body></html>"""


def _share_error_html(status: int, msg: str) -> HTMLResponse:
    page = (f"<!doctype html><meta charset='utf-8'>"
            f"<title>{status}</title>"
            f"<style>body{{font:14px sans-serif;padding:32px;color:#444}}</style>"
            f"<h1>{status}</h1><p>{html.escape(msg)}</p>")
    return HTMLResponse(page, status_code=status, headers=_SHARE_HTTP_HEADERS)


# ---- pygments inline highlighting ----

_pygments_formatter = None
_pygments_get_lexer = None


def _ensure_pygments():
    global _pygments_formatter, _pygments_get_lexer
    if _pygments_formatter is None:
        from pygments.formatters import HtmlFormatter  # noqa: F401
        from pygments.lexers import get_lexer_by_name, guess_lexer  # noqa: F401
        # noclasses=True inlines all styles → satisfies our strict CSP without
        # needing an extra <style> file the recipient must trust.
        _pygments_formatter = HtmlFormatter(
            style="monokai", noclasses=True, linenos=False, nowrap=False)
        def _get(lang: str):
            try:
                return get_lexer_by_name(lang, stripall=False)
            except Exception:
                from pygments.lexers.special import TextLexer
                return TextLexer()
        _pygments_get_lexer = _get


def _pygments_highlight(text: str, lang: str) -> str:
    _ensure_pygments()
    from pygments import highlight
    return highlight(text, _pygments_get_lexer(lang), _pygments_formatter)


def _wrap_tables(html_body: str) -> str:
    """Wrap each <table>...</table> in `<div class="md-table-scroll">` so wide
    tables get their own horizontal scrollbar instead of overflowing the page.
    Idempotent for tables already wrapped."""
    import re
    pat = re.compile(r"(<table\b[^>]*>.*?</table>)", re.DOTALL | re.IGNORECASE)
    return pat.sub(r'<div class="md-table-scroll">\1</div>', html_body)


def _highlight_code_blocks(html_body: str) -> str:
    """Pygments-highlight the <pre><code class="language-X"> blocks markdown
    emits via fenced_code. Anything we don't recognize stays as-is."""
    import re
    pat = re.compile(
        r'<pre><code class="language-([a-z0-9+_-]+)">(.*?)</code></pre>',
        re.DOTALL | re.IGNORECASE)

    def repl(m):
        lang = m.group(1)
        code = m.group(2)
        # The markdown lib HTML-escapes inside code; un-escape so pygments
        # gets the source. We escape again ourselves via highlight().
        unescaped = (code.replace("&lt;", "<").replace("&gt;", ">")
                          .replace("&amp;", "&").replace("&quot;", '"')
                          .replace("&#39;", "'"))
        return _pygments_highlight(unescaped, lang)

    return pat.sub(repl, html_body)


# ---- inline CSS (no external sheets, satisfies CSP) ----

def _share_page_css() -> str:
    return """
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
           'Helvetica Neue', Arial, 'Noto Sans CJK SC', sans-serif;
           background: #fafafa; color: #222; }
    .share-hd { display: flex; align-items: baseline; gap: 12px;
                padding: 10px 20px; background: #fff;
                border-bottom: 1px solid #e5e5e5; flex-wrap: wrap; }
    .share-hd .filename { font-weight: 600; }
    .share-hd .meta-label { color: #666; }
    .share-hd .meta-exp { margin-left: auto; color: #888; font-size: .9em; }
    .share-body { max-width: 920px; margin: 0 auto; padding: 24px 20px 64px; }
    .share-ft { padding: 12px 20px; color: #999; font-size: .8em;
                text-align: center; }
    .md h1, .md h2, .md h3 { line-height: 1.25; }
    .md h1 { border-bottom: 1px solid #eee; padding-bottom: 6px; }
    .md p, .md li { line-height: 1.65; }
    .md a { color: #1a6cba; }
    .md code { background: #f3f3f3; padding: 1px 5px; border-radius: 3px;
               font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
               font-size: .92em; }
    .md pre { padding: 12px 14px; border-radius: 6px; overflow-x: auto; }
    .md img { max-width: 100%; height: auto; border-radius: 4px; }
    .md-table-scroll { overflow-x: auto; max-width: 100%;
                       margin: 12px 0; -webkit-overflow-scrolling: touch; }
    .md-table-scroll table { margin: 0; }
    .md table { border-collapse: collapse; }
    .md th, .md td { border: 1px solid #ddd; padding: 6px 10px; }
    pre.text { background: #fff; padding: 14px 16px; border: 1px solid #eee;
               border-radius: 6px; overflow-x: auto; white-space: pre-wrap;
               word-wrap: break-word; font-family: ui-monospace, Menlo, monospace; }
    .img-wrap { text-align: center; }
    .img-wrap img { max-width: 100%; max-height: 80vh;
                    box-shadow: 0 2px 8px rgba(0,0,0,.1); border-radius: 6px; }
    """
