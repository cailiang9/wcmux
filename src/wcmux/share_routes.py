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

_RENDERABLE_TYPES = {"markdown", "code", "text", "image", "pdf", "drawio", "html"}

# Base headers shared by every /share response. CSP varies per file type
# (see `_share_headers`); the rest are constant.
_SHARE_BASE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

# Strict default — locks down rendered share HTML: no inline scripts, no
# external assets, only same-origin <img>/<iframe>/<object> (we serve them
# from /share/.../a or /share/.../raw) plus inline styles for syntax-
# highlighting / pygments output. `frame-src 'self'` admits same-origin
# iframes; `object-src 'self'` admits same-origin <object>/<embed> (used
# by PDF shares — without this directive, default-src 'none' would block
# the PDF embed and the desktop browser would silently fall through to
# the `<object>`'s inner fallback content).
_CSP_STRICT = (
    "default-src 'none'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; script-src 'none'; "
    "frame-src 'self'; object-src 'self'; "
    "frame-ancestors 'none';"
)

# drawio shares need to (a) load https://embed.diagrams.net inside an iframe
# and (b) run a small inline <script> that postMessages the XML payload to
# that iframe on the embed-protocol 'init' event. So we relax script-src to
# allow inline + frame-src to whitelist the drawio embed origin. No new
# external connect-src (the iframe handles its own asset fetches under its
# own origin's CSP, not ours).
_CSP_DRAWIO = (
    "default-src 'none'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "frame-src 'self' https://embed.diagrams.net; "
    "frame-ancestors 'none';"
)

# HTML shares serve the source HTML inside a sandboxed <iframe> from the
# same origin (path-based /share/.../raw/<path>) so relative <img>/<video>/
# <audio>/<link>/<script> refs inside the HTML resolve to sibling files
# served by the same route. The iframe sandbox attribute (set on the iframe
# element, not via CSP) restricts what the HTML can do; the outer page
# itself stays script-free.
_CSP_HTML = (
    "default-src 'none'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; script-src 'none'; "
    "frame-src 'self'; frame-ancestors 'none';"
)


def _share_headers(ftype: str) -> dict:
    if ftype == "drawio":
        csp = _CSP_DRAWIO
    elif ftype == "html":
        csp = _CSP_HTML
    else:
        csp = _CSP_STRICT
    return {**_SHARE_BASE_HEADERS, "Content-Security-Policy": csp}


# Back-compat alias for asset/raw routes that don't need per-ftype CSP.
_SHARE_HTTP_HEADERS = {**_SHARE_BASE_HEADERS, "Content-Security-Policy": _CSP_STRICT}

# Headers for /raw and /raw/{path:path} responses that the share PAGE itself
# embeds via <iframe>/<img>/<video>. We need:
#   1. CSP `frame-ancestors 'self'` (modern browsers) so our share page can
#      frame these bytes, but foreign origins still can't.
#   2. **Override** `X-Frame-Options: DENY` (inherited from base) to
#      `SAMEORIGIN`. Older browsers ignore CSP frame-ancestors and rely on
#      X-Frame-Options; with DENY left in place, iframe load gets blocked
#      and the browser falls back to navigating directly — for PDFs that
#      means an auto-download dialog (the bug we're fixing).
_SHARE_RAW_HEADERS = {
    **_SHARE_BASE_HEADERS,
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": "frame-ancestors 'self';",
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

    # Refuse to create shares for files whose path includes any hidden
    # segment (`.env`, `.bash_history`, `.aws/credentials`, `.ssh/id_rsa`,
    # …). These are almost never something the creator means to expose;
    # accidentally pasting such a share URL into Slack would leak secrets.
    # Listing / search already filter dotfiles, but `path=` is a direct
    # entry point — block here at the create-share boundary.
    if any(part.startswith(".") for part in target.parts):
        raise HTTPException(
            status_code=415,
            detail="cannot share hidden files / files under hidden directories")

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
    return HTMLResponse(page, headers=_share_headers(ftype))


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
    return FileResponse(real, filename=real.name,
                        content_disposition_type="inline",
                        headers=dict(_SHARE_RAW_HEADERS))


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
        if any(part.startswith(".") for part in real.parts):
            skipped.append({"path": raw_url, "reason": "hidden file or hidden ancestor dir"})
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

    if ftype == "pdf":
        from urllib.parse import quote
        share_base = _share_path(share)
        url = share["source_url_path"]
        raw_url = f'{share_base}/raw?path={quote(url, safe="")}'
        raw_url_esc = html.escape(raw_url)
        # `<object>` instead of `<iframe>`: when the browser can't render
        # the embedded PDF (notably iOS Safari / Android Chrome inside an
        # iframe), `<object>` falls back to its inner content automatically.
        # Below the object we always render a "open in new tab" link — on
        # mobile this is the only reliable path (system PDF viewer takes
        # over once the URL is the top-level navigation, which `<object>`
        # / `<iframe>` embeds aren't).
        return (
            f'<div class="pdf-wrap">'
            f'<object data="{raw_url_esc}" type="application/pdf">'
            f'<p class="pdf-fallback-inner">'
            f'此浏览器无法内嵌渲染 PDF。'
            f'<a href="{raw_url_esc}" target="_blank" rel="noopener">在新标签页打开 PDF</a>'
            f'</p>'
            f'</object>'
            f'<p class="pdf-fallback">'
            f'移动端 / 嵌入查看不便？'
            f'<a href="{raw_url_esc}" target="_blank" rel="noopener">在新标签页打开 PDF</a>'
            f'</p>'
            f'</div>'
        )

    if ftype == "drawio":
        # Inline the .drawio XML inside a hidden <pre> (HTML-escaped); the
        # script reads its textContent (which un-escapes back to original
        # XML) and ships it to embed.diagrams.net via postMessage on init.
        # CSP for drawio shares allows inline-script + the embed origin
        # (see `_CSP_DRAWIO`); no external CDN, no fetch.
        try:
            xml = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            xml = ""
        escaped_xml = html.escape(xml)
        return (
            f'<div class="drawio-wrap">'
            f'<pre id="drawio-xml" style="display:none">{escaped_xml}</pre>'
            f'<iframe id="drawio-frame" title="drawio diagram" '
            f'src="https://embed.diagrams.net/?embed=1&proto=json&spin=1&dark=auto"></iframe>'
            f'</div>'
            f'<script>'
            f'(function(){{'
            f'var xml=document.getElementById("drawio-xml").textContent;'
            f'var f=document.getElementById("drawio-frame");'
            f'window.addEventListener("message",function(e){{'
            f'if(e.origin!=="https://embed.diagrams.net")return;'
            f'var m;try{{m=JSON.parse(e.data);}}catch(_){{return;}}'
            f'if(m.event==="init"){{'
            f'f.contentWindow.postMessage(JSON.stringify({{'
            f'action:"load",xml:xml,autosave:0,saveAndExit:0,noSaveBtn:1,noExitBtn:1'
            f'}}),"https://embed.diagrams.net");'
            f'}}'
            f'}});'
            f'}})();'
            f'</script>'
        )

    if ftype == "html":
        from urllib.parse import quote
        share_base = _share_path(share)
        url = share["source_url_path"]
        # Path-based iframe URL: <img src="x.png"> in the HTML resolves to
        # /share/.../raw/<dir-of-source>/x.png and hits `share_raw_tree`,
        # which serves the sibling file. Absolute source paths (under extra
        # roots) can't ride the path-based route — fall back to the query-
        # param raw endpoint (page loads, but relative refs inside won't).
        if url.startswith("/"):
            iframe_src = f'{share_base}/raw?path={quote(url, safe="")}'
        else:
            path_encoded = "/".join(quote(s, safe="") for s in url.split("/"))
            iframe_src = f'{share_base}/raw/{path_encoded}'
        return (
            f'<div class="html-wrap">'
            f'<iframe title="" src="{html.escape(iframe_src)}" '
            f'sandbox="allow-scripts allow-popups allow-forms"></iframe>'
            f'</div>'
        )

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
    return FileResponse(real, filename=real.name,
                        content_disposition_type="inline",
                        headers=dict(_SHARE_RAW_HEADERS))


@router.get("/share/{date}/{seg}/raw/{path:path}")
def share_raw_tree(date: str, seg: str, path: str,
                   request: Request) -> FileResponse:
    """Path-based asset endpoint **for HTML shares only**. The HTML iframe
    loads from this route, so `<img src="figure.png">` inside the HTML
    resolves to /share/.../raw/<dir>/figure.png and hits this same route.

    The route is gated by source ftype: if the share's source isn't HTML
    we refuse with 404, even though the route's URL pattern is identical
    across all share types. This prevents a non-HTML share's URL (whose
    holder is only entitled to ONE file by the share contract) from being
    weaponized to enumerate `<source-parent>/credentials.json` etc.

    Scope (for HTML shares): target file must lie within the source's
    parent directory (or deeper) on the same preview root. Hidden files
    (`.foo`) are blocked to prevent ref-based exfiltration of dotfiles
    (`.env`, `.git/config`, …). Files of file_type=='unknown' are refused
    so the route never turns into a generic anything-server."""
    from .preview import (
        _preview_roots, file_type as _file_type, resolve_path as _resolve,
    )
    sid, share = _lookup_or_4xx(date, seg, request)
    # Gate: only HTML-typed shares may dereference siblings via this route.
    # Other types route raw fetches through the strict /raw?path= endpoint
    # (which requires path == source_url_path, so no sibling exposure).
    src = Path(share["source_real_path"]).resolve(strict=False)
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=410, detail="source no longer exists")
    if _file_type(src) != "html":
        raise HTTPException(status_code=404)
    roots = _preview_roots(request)
    target = _resolve(roots, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404)
    if any(part.startswith(".") for part in target.parts):
        raise HTTPException(status_code=404)
    ftype = _file_type(target)
    if ftype == "unknown":
        raise HTTPException(status_code=404)
    # Scope to source's parent dir (or its descendants). Source itself is
    # also allowed (the iframe's own src lands here on first request).
    src_dir = src.parent
    target_real = target.resolve(strict=False)
    try:
        target_real.relative_to(src_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="outside share scope")
    return FileResponse(target_real, filename=target_real.name,
                        content_disposition_type="inline",
                        headers=dict(_SHARE_RAW_HEADERS))


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


# ---- pygments highlighting (class-based; light + dark CSS in page <style>) ----

_pygments_formatter_light = None
_pygments_formatter_dark = None
_pygments_get_lexer = None


def _ensure_pygments():
    global _pygments_formatter_light, _pygments_formatter_dark, _pygments_get_lexer
    if _pygments_formatter_light is None:
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name
        # class-based output (`noclasses=False`) so we can ship two CSS rule
        # sets in the page <style>: a light one as the default plus a dark one
        # under prefers-color-scheme: dark. Both formatters emit identical
        # `cssclass="highlight"` markup → same DOM, just different styling.
        # linenos='inline' + linespans='line': each source line is wrapped in
        # `<span id="line-N" class="line"><span class="lineno">N</span>code</span>`.
        # With CSS `.line { display: block; padding-left: Xem; text-indent: -Xem }`,
        # wrapped continuations stay aligned past the lineno gutter — what
        # `linenos='table'` cannot do, because numbers/code live in separate <pre>s
        # and any wrap in code drifts the columns out of sync.
        _pygments_formatter_light = HtmlFormatter(
            style="default", noclasses=False, linenos="inline", nowrap=False,
            cssclass="highlight", linespans="line")
        _pygments_formatter_dark = HtmlFormatter(
            style="monokai", noclasses=False, linenos="inline", nowrap=False,
            cssclass="highlight", linespans="line")
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
    return highlight(text, _pygments_get_lexer(lang), _pygments_formatter_light)


def _pygments_css_pair() -> tuple[str, str]:
    """(light_rules, dark_rules), both scoped to .highlight."""
    _ensure_pygments()
    return (_pygments_formatter_light.get_style_defs(".highlight"),
            _pygments_formatter_dark.get_style_defs(".highlight"))


def _wrap_tables(html_body: str) -> str:
    """Wrap each markdown <table>...</table> in `<div class="md-table-scroll">`
    so wide tables get their own horizontal scrollbar instead of overflowing
    the page. Skips pygments `.highlighttable` (line-number layout — already
    has its own width handling and shouldn't get a scroll wrapper)."""
    import re
    pat = re.compile(r"(<table\b[^>]*>)(.*?)(</table>)", re.DOTALL | re.IGNORECASE)
    def repl(m):
        opening = m.group(1)
        if "highlighttable" in opening:
            return m.group(0)
        return f'<div class="md-table-scroll">{m.group(0)}</div>'
    return pat.sub(repl, html_body)


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
    """Light by default; dark only when the recipient's OS prefers it. CSS
    variables drive color choices so the rest of the rules stay theme-agnostic.
    Pygments rule sets piggy-back: light at top level, dark inside the same
    @media (prefers-color-scheme: dark) block."""
    light_pyg, dark_pyg = _pygments_css_pair()
    return f"""
    :root {{
      --bg: #fafafa; --hd-bg: #fff; --fg: #222; --fg-muted: #666;
      --fg-faint: #888; --footer: #999; --bd: #e5e5e5; --bd-soft: #eee;
      --code-bg: #f3f3f3; --link: #0969da; --shadow: rgba(0,0,0,.10);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0d1117; --hd-bg: #161b22; --fg: #c9d1d9; --fg-muted: #8b949e;
        --fg-faint: #768390; --footer: #6e7681; --bd: #30363d; --bd-soft: #21262d;
        --code-bg: #161b22; --link: #58a6ff; --shadow: rgba(0,0,0,.55);
      }}
    }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
           'Helvetica Neue', Arial, 'Noto Sans CJK SC', sans-serif;
           background: var(--bg); color: var(--fg); }}
    .share-hd {{ display: flex; align-items: baseline; gap: 12px;
                padding: 10px 20px; background: var(--hd-bg);
                border-bottom: 1px solid var(--bd); flex-wrap: wrap; }}
    .share-hd .filename {{ font-weight: 600; }}
    .share-hd .meta-label {{ color: var(--fg-muted); }}
    .share-hd .meta-exp {{ margin-left: auto; color: var(--fg-faint); font-size: .9em; }}
    .share-body {{ max-width: 920px; margin: 0 auto; padding: 24px 20px 64px; }}
    .share-ft {{ padding: 12px 20px; color: var(--footer); font-size: .8em;
                text-align: center; }}
    .md h1, .md h2, .md h3 {{ line-height: 1.25; }}
    .md h1 {{ border-bottom: 1px solid var(--bd-soft); padding-bottom: 6px; }}
    .md p, .md li {{ line-height: 1.65; }}
    .md a {{ color: var(--link); }}
    .md code {{ background: var(--code-bg); padding: 1px 5px; border-radius: 3px;
               font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
               font-size: .92em; }}
    .md pre {{ padding: 12px 14px; border-radius: 6px;
               background: var(--code-bg); border: 1px solid var(--bd-soft);
               white-space: pre-wrap; word-break: break-word; overflow-x: auto; }}
    .md pre code {{ background: none; padding: 0;
                   white-space: inherit; word-break: inherit; }}
    /* pygments linenos='inline' + linespans='line':
         <pre>
           <span id="line-1"><span class="linenos">1</span>code</span>
           ...
         </pre>
       Each source line is a span with id="line-N"; `display:block` +
       `padding-left + text-indent` makes wrap continuations visually flow
       past the lineno gutter without their own lineno span. .linenos is
       unselectable so copy-paste skips line numbers. */
    .highlight pre {{ white-space: pre-wrap; word-break: break-word;
                     overflow-x: auto; padding: 12px 14px; }}
    .highlight pre > span[id^="line-"] {{ display: block;
                                         padding-left: 4em;
                                         text-indent: -4em; }}
    .highlight .linenos {{ display: inline-block; width: 3em;
                          margin-right: 0.5em; padding-right: 0.5em;
                          text-align: right; color: var(--fg-faint);
                          border-right: 1px solid var(--bd-soft);
                          user-select: none; -webkit-user-select: none;
                          font-variant-numeric: tabular-nums;
                          background: transparent !important; }}
    .md img {{ max-width: 100%; height: auto; border-radius: 4px; }}
    .md-table-scroll {{ overflow-x: auto; max-width: 100%;
                       margin: 12px 0; -webkit-overflow-scrolling: touch; }}
    .md-table-scroll table {{ margin: 0; }}
    .md table {{ border-collapse: collapse; }}
    .md th, .md td {{ border: 1px solid var(--bd); padding: 6px 10px; }}
    pre.text {{ background: var(--hd-bg); padding: 14px 16px; border: 1px solid var(--bd-soft);
               border-radius: 6px; overflow-x: auto; white-space: pre-wrap;
               word-wrap: break-word; font-family: ui-monospace, Menlo, monospace; }}
    .img-wrap {{ text-align: center; }}
    .img-wrap img {{ max-width: 100%; max-height: 80vh;
                    box-shadow: 0 2px 8px var(--shadow); border-radius: 6px; }}
    .pdf-wrap {{ width: 100%; }}
    .pdf-wrap object {{ width: 100%; height: 85vh; border: 1px solid var(--bd);
                       border-radius: 6px; background: #fff;
                       box-shadow: 0 2px 8px var(--shadow); display: block; }}
    .pdf-wrap .pdf-fallback-inner {{ padding: 32px; text-align: center;
                                    color: var(--fg-muted); }}
    .pdf-wrap .pdf-fallback {{ display: none; }}
    /* Show the "open in new tab" hint only on coarse pointers (touchscreens
       — phones / tablets). Desktop with mouse hides it: <object> already
       renders PDF inline reliably there, so the hint would just be noise. */
    @media (pointer: coarse) {{
      .pdf-wrap .pdf-fallback {{ display: block; margin-top: 12px;
                                padding: 10px 14px; background: var(--code-bg);
                                border: 1px solid var(--bd-soft);
                                border-radius: 6px; text-align: center;
                                font-size: .9em; color: var(--fg-muted); }}
      .pdf-wrap .pdf-fallback a {{ color: var(--link); font-weight: 600;
                                  margin-left: 6px; }}
    }}
    .drawio-wrap {{ width: 100%; }}
    .drawio-wrap iframe {{ width: 100%; height: 85vh; border: 1px solid var(--bd);
                          border-radius: 6px; background: #fff;
                          box-shadow: 0 2px 8px var(--shadow); }}
    .html-wrap {{ width: 100%; }}
    .html-wrap iframe {{ width: 100%; height: 85vh; border: 1px solid var(--bd);
                        border-radius: 6px; background: #fff;
                        box-shadow: 0 2px 8px var(--shadow); }}
    /* Pygments — light rule set is the baseline; the dark one overrides via
       prefers-color-scheme. Both reuse the same `.highlight` class names. */
    {light_pyg}
    @media (prefers-color-scheme: dark) {{
    {dark_pyg}
    }}
    """
