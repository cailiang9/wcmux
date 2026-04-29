"""Preview sub-router (spec §4.22): in-process port of mdpreview.

All routes require an authenticated wcmux session (Depends(require_auth)).
Paths are namespaced under /api/preview/* and /raw/preview/* so they can
co-exist with wcmux's own /api/tabs and /ws/{tab_id} routes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import markdown
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .auth import require_auth

# ---- file type recognition (ported from mdpreview) ----

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tiff", ".avif"}
HTML_EXTS = {".html", ".htm"}
DRAWIO_EXTS = {".drawio"}
JSONL_EXTS = {".jsonl", ".ndjson"}
MARKDOWN_EXTS = {".md", ".markdown"}
CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
    ".java", ".rb", ".php", ".swift", ".kt", ".sh", ".bash", ".zsh", ".fish",
    ".css", ".scss", ".less", ".xml", ".json", ".yaml", ".yml",
    ".toml", ".sql", ".r", ".lua", ".vim", ".tf", ".proto", ".graphql",
    ".dockerfile", ".makefile", ".cmake", ".gradle", ".ini", ".cfg",
}
TEXT_EXTS = {".txt", ".log", ".csv", ".tsv", ".env", ".conf", ".gitignore", ".editorconfig"}

LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "jsx", ".tsx": "tsx", ".go": "go", ".rs": "rust",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".java": "java", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
    ".css": "css", ".scss": "scss", ".less": "less", ".html": "html",
    ".xml": "xml", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".sql": "sql", ".r": "r", ".lua": "lua",
    ".tf": "hcl", ".proto": "protobuf", ".graphql": "graphql",
    ".ini": "ini", ".cfg": "ini",
}

JSONL_TRUNCATE_THRESHOLD = 10000
JSONL_TRUNCATE_TO = 1000

# Skip these dirs during search; they're rarely useful and explode walk time.
SEARCH_DENY_DIRS = {
    "node_modules", "__pycache__", ".venv", ".git", ".cache",
    ".local", ".npm", ".cargo", ".rustup", ".tox", "dist", "build",
}
SEARCH_MAX_DEPTH = 2  # spec §4.22: file's parent dir at depth ≤ 2 from root


def file_type(p: Path) -> str:
    ext = p.suffix.lower()
    name = p.name.lower()
    if ext in IMAGE_EXTS: return "image"
    if ext in HTML_EXTS: return "html"
    if ext in DRAWIO_EXTS: return "drawio"
    if ext in JSONL_EXTS: return "jsonl"
    if ext in MARKDOWN_EXTS: return "markdown"
    if ext in CODE_EXTS or name in {"dockerfile", "makefile", "gemfile", "rakefile"}:
        return "code"
    if ext in TEXT_EXTS: return "text"
    return "unknown"


def format_size(size: int) -> str:
    s = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if s < 1024:
            return f"{int(s)} B" if unit == "B" else f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} TB"


def format_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _preview_roots(request: Request) -> list[Path]:
    return getattr(request.app.state, "preview_roots", [request.app.state.preview_root])


def _under_any(p: Path, roots: list[Path]) -> Path | None:
    """Return the first root that contains `p` (lexically, before resolve), or
    None. Used to gate URL→FS mapping."""
    for r in roots:
        try:
            p.relative_to(r)
            return r
        except ValueError:
            continue
    return None


def resolve_path(roots: list[Path], rel: str) -> Path:
    """Map a URL `path` to a filesystem path. The URL form is one of:
      - relative (e.g. ``foo/bar.md``): anchored to the primary root (roots[0]).
      - absolute (e.g. ``/media/orangepi/foo.png``): accepted iff lexically under
        any of `roots`; lets users address mounted-elsewhere extra roots.

    `..` traversal is rejected (the unresolved candidate must be lexically under
    some root). Symlinks under a root are trusted (mdpreview policy) so the
    final returned path is `joined.resolve()`."""
    if rel.startswith("/"):
        joined = Path(rel)
    else:
        joined = roots[0] / rel
    if _under_any(joined, roots) is None:
        raise HTTPException(status_code=403, detail="Access denied")
    return joined.resolve()


def to_url_path(p: Path, roots: list[Path]) -> str:
    """Map a filesystem path back into the form accepted by `resolve_path`.
    Files under the primary root are rendered relative (shorter URLs, common
    case); files under an extra root come back absolute."""
    primary = roots[0]
    try:
        return str(p.relative_to(primary)).replace(os.sep, "/")
    except ValueError:
        # absolute path; preserve the leading slash (str(Path('/a/b')) == '/a/b')
        return str(p).replace(os.sep, "/")


# ---- router ----

router = APIRouter()


@router.get("/api/preview/list")
def api_list(request: Request,
             path: str = Query(default=""),
             _=Depends(require_auth)) -> JSONResponse:
    roots = _preview_roots(request)
    if path == "":
        target = roots[0]
        # logical base used to compute item paths; for the empty (primary root)
        # case this matches resolve_path("") behavior.
        logical_base = roots[0]
    else:
        target = resolve_path(roots, path)
        # logical_base mirrors resolve_path's joining rule but pre-resolve, so
        # entries under symlinked roots get URL paths relative to the URL root,
        # not the symlink target.
        logical_base = (Path(path) if path.startswith("/")
                        else roots[0] / path.lstrip("/"))
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")

    items = []
    try:
        entries = list(target.iterdir())
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=403, detail=str(e))

    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        url_path = to_url_path(logical_base / name, roots)
        if entry.is_dir():
            items.append({
                "kind": "dir",
                "name": name,
                "path": url_path,
                "mtime": format_time(st.st_mtime),
                "_sort": (0, name.lower()),
            })
        elif entry.is_file():
            ftype = file_type(entry)
            if ftype == "unknown":
                continue
            items.append({
                "kind": ftype,
                "name": name,
                "path": url_path,
                "size": format_size(st.st_size),
                "mtime": format_time(st.st_mtime),
                "_sort": (1, name.lower()),
            })

    items.sort(key=lambda d: d["_sort"])
    for it in items:
        it.pop("_sort", None)

    # Breadcrumb: walk segments while keeping the absolute-or-relative form
    # consistent with how `path` came in, so link clicks round-trip cleanly.
    breadcrumb = []
    if path:
        is_abs = path.startswith("/")
        segs = path.strip("/").split("/")
        accum = ""
        for seg in segs:
            accum = f"{accum}/{seg}" if accum else seg
            breadcrumb.append({
                "name": seg,
                "path": ("/" + accum) if is_abs else accum,
            })

    return JSONResponse({"breadcrumb": breadcrumb, "items": items})


@router.get("/api/preview/file")
def api_file(request: Request,
             path: str = Query(...),
             _=Depends(require_auth)) -> JSONResponse:
    roots = _preview_roots(request)
    target = resolve_path(roots, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    ftype = file_type(target)

    if ftype == "image":
        return JSONResponse({"type": "image", "path": path})
    if ftype == "html":
        return JSONResponse({"type": "html", "path": path})

    if ftype == "drawio":
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError) as e:
            raise HTTPException(status_code=403, detail=str(e))
        return JSONResponse({"type": "drawio", "content": content, "path": path})

    if ftype == "unknown":
        raise HTTPException(status_code=404, detail="Unsupported file type")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=403, detail=str(e))

    if ftype == "jsonl":
        indexed = [
            (idx, raw)
            for idx, raw in enumerate(content.splitlines(), start=1)
            if raw.strip() != ""
        ]
        total = len(indexed)
        truncated = total >= JSONL_TRUNCATE_THRESHOLD
        visible = indexed[:JSONL_TRUNCATE_TO] if truncated else indexed
        records = []
        for idx, raw in visible:
            try:
                records.append({"line": idx, "ok": True, "json": json.loads(raw)})
            except json.JSONDecodeError as e:
                records.append({"line": idx, "ok": False, "raw": raw, "error": str(e)})
        return JSONResponse({
            "type": "jsonl",
            "records": records,
            "truncated": truncated,
            "total": total,
        })

    if ftype == "markdown":
        html = markdown.markdown(
            content,
            extensions=["fenced_code", "tables", "toc", "nl2br", "sane_lists", "attr_list"],
        )
        return JSONResponse({"type": "markdown", "html": html})

    if ftype == "code":
        lang = LANG_MAP.get(target.suffix.lower(), "plaintext")
        return JSONResponse({"type": "code", "content": content, "lang": lang})

    return JSONResponse({"type": "text", "content": content})


@router.post("/api/preview/save")
async def api_save(request: Request, _=Depends(require_auth)) -> JSONResponse:
    roots = _preview_roots(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    path = (body or {}).get("path", "")
    content = (body or {}).get("content", "")
    if not path or not isinstance(path, str):
        raise HTTPException(status_code=400, detail="Missing path")
    target = resolve_path(roots, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    ftype = file_type(target)
    if ftype != "drawio":
        raise HTTPException(status_code=403, detail="Only drawio files can be saved")
    try:
        target.write_text(content, encoding="utf-8")
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=403, detail=str(e))
    return JSONResponse({"ok": True})


@router.get("/api/preview/search")
def api_search(request: Request,
               q: str = Query(default=""),
               limit: int = Query(default=50, ge=1, le=200),
               _=Depends(require_auth)) -> JSONResponse:
    """Spec §4.22: name substring search across the primary root and all
    extra roots, each capped at SEARCH_MAX_DEPTH levels of nested dirs,
    skipping hidden + deny-listed. Matches both directory names (type='dir')
    and file names (type=<ftype>). Empty q returns []."""
    if not q:
        return JSONResponse({"results": []})
    roots = _preview_roots(request)
    q_lower = q.lower()
    out: list[dict] = []

    for root in roots:
        out.extend(_search_in_root(root, roots, q_lower))

    out.sort(key=lambda r: r["mtime"], reverse=True)
    return JSONResponse({"results": out[:limit]})


def _search_in_root(root: Path, roots: list[Path], q_lower: str) -> list[dict]:
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        try:
            rel = Path(dirpath).relative_to(root)
        except ValueError:
            continue
        depth = 0 if str(rel) == "." else len(rel.parts)
        # Filter children we'll descend into: drop hidden + deny-listed.
        # Match dir names BEFORE pruning, so a directory whose own name matches
        # but is itself deny-listed (e.g. user typed "node") is excluded.
        kept_dirs = [
            d for d in dirnames
            if not d.startswith(".") and d not in SEARCH_DENY_DIRS
        ]
        # A child dir's own depth is depth+1; only return it as a match if it
        # fits within the SEARCH_MAX_DEPTH budget (otherwise we'd surface dirs
        # users can't even see in the file browser).
        if depth < SEARCH_MAX_DEPTH:
            for d in kept_dirs:
                if q_lower in d.lower():
                    p = Path(dirpath) / d
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    out.append({
                        "path": to_url_path(p, roots),
                        "name": d,
                        "type": "dir",
                        "size": "",
                        "mtime": st.st_mtime,
                        "mtime_display": format_time(st.st_mtime),
                    })
        dirnames[:] = kept_dirs
        # Don't recurse deeper than SEARCH_MAX_DEPTH levels of dirs from root.
        if depth >= SEARCH_MAX_DEPTH:
            dirnames[:] = []
        for fname in filenames:
            if fname.startswith("."):
                continue
            if q_lower not in fname.lower():
                continue
            p = Path(dirpath) / fname
            ftype = file_type(p)
            if ftype == "unknown":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "path": to_url_path(p, roots),
                "name": fname,
                "type": ftype,
                "size": format_size(st.st_size),
                "mtime": st.st_mtime,
                "mtime_display": format_time(st.st_mtime),
            })
    return out


@router.get("/raw/preview")
def raw_file(request: Request,
             path: str = Query(...),
             _=Depends(require_auth)) -> FileResponse:
    """Serve image / html / drawio / jsonl / etc. raw bytes; used by
    preview.html for <img src> and <iframe src>. Path is a query param (rather
    than a URL segment) so absolute paths under extra roots survive HTTP path
    normalization (a leading `/` collapsed to `//` would otherwise vanish).
    Refuses unsupported types so we don't accidentally turn into a generic
    file server."""
    roots = _preview_roots(request)
    target = resolve_path(roots, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404)
    ftype = file_type(target)
    if ftype == "unknown":
        raise HTTPException(status_code=404)
    return FileResponse(target)
