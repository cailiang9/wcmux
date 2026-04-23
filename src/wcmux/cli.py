from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from .config import Config, default_shell


def _env(*keys: str) -> Optional[str]:
    for k in keys:
        v = os.environ.get(k)
        if v is not None and v != "":
            return v
    return None


def _parse_port(raw: str) -> int:
    try:
        p = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"port must be an integer, got {raw!r}")
    if not (1 <= p <= 65535):
        raise argparse.ArgumentTypeError(f"port out of range 1..65535: {p}")
    return p


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wcmux",
        description="Web-based cmux — FastAPI terminal multiplexer with tabs.",
    )
    p.add_argument("--port", type=_parse_port, default=None,
                   help="HTTP listen port (env WCMUX_PORT, default 8022)")
    p.add_argument("--host", default=None,
                   help="Bind host (env WCMUX_HOST, default 0.0.0.0)")
    p.add_argument("--base-url", default=None,
                   help="URL path prefix for reverse proxy (env WCMUX_BASE_URL, default '')")
    p.add_argument("--password", default=None,
                   help="Login password (env WCMUX_PASSWORD; required)")
    p.add_argument("--shell", default=None,
                   help=f"Shell executable for new tabs (env WCMUX_SHELL, default {default_shell()!r})")
    p.add_argument("--secret-key", default=None,
                   help="Session signing key (env WCMUX_SECRET_KEY; random if unset)")
    p.add_argument("--trust-proxy", action="store_true", default=None,
                   help="Honor X-Forwarded-* headers (env WCMUX_TRUST_PROXY=1)")
    return p


def resolve_config(argv: Optional[list[str]] = None) -> Config:
    args = build_parser().parse_args(argv)

    password = args.password if args.password is not None else _env("WCMUX_PASSWORD")
    if not password:
        sys.stderr.write(
            "error: password is required. Set --password or WCMUX_PASSWORD.\n"
        )
        sys.exit(2)

    port_raw = args.port if args.port is not None else _env("WCMUX_PORT")
    if port_raw is None:
        port = 8022
    elif isinstance(port_raw, int):
        port = port_raw
    else:
        try:
            port = int(port_raw)
        except ValueError:
            sys.stderr.write(f"error: WCMUX_PORT must be integer, got {port_raw!r}\n")
            sys.exit(2)
        if not (1 <= port <= 65535):
            sys.stderr.write(f"error: port out of range 1..65535: {port}\n")
            sys.exit(2)

    host = args.host if args.host is not None else (_env("WCMUX_HOST") or "0.0.0.0")
    base_url = args.base_url if args.base_url is not None else (_env("WCMUX_BASE_URL") or "")
    shell = args.shell if args.shell is not None else (_env("WCMUX_SHELL") or default_shell())
    secret_key = args.secret_key if args.secret_key is not None else (_env("WCMUX_SECRET_KEY") or "")
    trust_proxy = args.trust_proxy if args.trust_proxy is not None else bool(_env("WCMUX_TRUST_PROXY"))

    return Config(
        password=password,
        port=port,
        host=host,
        base_url=base_url,
        shell=shell,
        secret_key=secret_key,
        trust_proxy=bool(trust_proxy),
    )


def main(argv: Optional[list[str]] = None) -> None:
    config = resolve_config(argv)
    if config.secret_key_generated:
        sys.stderr.write(
            "warning: WCMUX_SECRET_KEY not set; using a random key. "
            "Sessions will be invalidated on restart.\n"
        )
    # Import lazily so --help works without FastAPI installed
    import uvicorn
    from .app import create_app

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        proxy_headers=config.trust_proxy,
        forwarded_allow_ips="*" if config.trust_proxy else None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
