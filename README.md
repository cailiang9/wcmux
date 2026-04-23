# wcmux

Web-based cmux — FastAPI terminal multiplexer with tabs. Password-protected,
multi-tab browser terminal with configurable port and base URL (for reverse-
proxy subpaths).

Supported platforms: Linux, macOS, Windows 10 1809+. Python ≥ 3.10.

## Install

```bash
# With uv (recommended)
uv venv .venv --python 3.10
uv pip install -e .

# Or plain pip
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

## Run

```bash
wcmux --password <your-password>
# or
WCMUX_PASSWORD=<pw> python -m wcmux
```

Browse to `http://<host>:8022/` and sign in.

## Configuration

All options also read from env vars. CLI args win over env vars.

| CLI | env | default |
|---|---|---|
| `--port` | `WCMUX_PORT` | `8022` |
| `--host` | `WCMUX_HOST` | `0.0.0.0` |
| `--base-url` | `WCMUX_BASE_URL` | `""` (no prefix) |
| `--password` | `WCMUX_PASSWORD` | **required** |
| `--shell` | `WCMUX_SHELL` | `$SHELL` / `/bin/bash` (Unix), `%COMSPEC%` / `powershell.exe` (Win) |
| `--secret-key` | `WCMUX_SECRET_KEY` | random (warns; sessions invalidated on restart) |
| `--trust-proxy` | `WCMUX_TRUST_PROXY=1` | off |

Run `wcmux --help` for the authoritative list.

## Hotkeys

| Keys | Action |
|---|---|
| `Ctrl+Alt+T` | New tab |
| `Ctrl+Alt+W` | Close current tab |
| `Ctrl+Alt+←` / `Ctrl+Alt+→` | Prev / next tab (cyclic) |
| `Ctrl+Alt+1` … `Ctrl+Alt+9` | Jump to tab by index |

## Reverse proxy (Nginx)

Mount under a subpath like `/wcmux/`:

```nginx
location /wcmux/ {
    proxy_pass         http://127.0.0.1:8022/;
    proxy_http_version 1.1;
    proxy_set_header   Upgrade $http_upgrade;
    proxy_set_header   Connection "upgrade";
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_set_header   X-Forwarded-Host $host;
    proxy_read_timeout 3600s;
}
```

Start wcmux with the matching prefix:

```bash
wcmux --password ... --base-url /wcmux --trust-proxy
```

## Spec, testing, and implementation plan

Under `docs/`:

- `spec-YYYYMMDD.md` — requirements
- `testing-YYYYMMDD.md` — verification checklist
- `plan-YYYYMMDD.md` — implementation plan

## Tests

Integration tests live in `tests/`. Each milestone has a dedicated script.
`tests/runserver.sh` starts a fresh server per script so state (e.g. login
lockouts) doesn't bleed across tests.

```bash
tests/runserver.sh tests/test_m1_auth.py tests/test_m2_single_tab.py \
                   tests/test_m3_multitab.py tests/test_m4_cwd.py
.venv/bin/python tests/test_m4_shorten.py
.venv/bin/python tests/test_m5_baseurl.py
```
