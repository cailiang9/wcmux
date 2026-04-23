from __future__ import annotations

import argparse
import getpass
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
        description=(
            "Web-based cmux — FastAPI terminal multiplexer with tabs.\n\n"
            "Subcommands:\n"
            "  hash-password    generate an argon2id hash for a password (stdin-safe)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--port", type=_parse_port, default=None,
                   help="HTTP listen port (env WCMUX_PORT, default 8022)")
    p.add_argument("--host", default=None,
                   help="Bind host (env WCMUX_HOST, default 0.0.0.0)")
    p.add_argument("--base-url", default=None,
                   help="URL path prefix for reverse proxy (env WCMUX_BASE_URL, default '')")
    p.add_argument("--password", default=None,
                   help="Login password, plaintext (env WCMUX_PASSWORD). "
                        "Prefer --password-hash; plaintext is hashed at startup and a warning is logged.")
    p.add_argument("--password-hash", default=None,
                   help="Pre-computed argon2id / bcrypt hash (env WCMUX_PASSWORD_HASH). "
                        "Wins over --password when both are set.")
    p.add_argument("--shell", default=None,
                   help=f"Shell executable for new tabs (env WCMUX_SHELL, default {default_shell()!r})")
    p.add_argument("--secret-key", default=None,
                   help="Session signing key (env WCMUX_SECRET_KEY; random if unset)")
    p.add_argument("--trust-proxy", action="store_true", default=None,
                   help="Honor X-Forwarded-* headers (env WCMUX_TRUST_PROXY=1)")
    return p


def _resolve_password_hash(args) -> str:
    """Return the argon2id/bcrypt hash to use. Exit(2) on unrecoverable config errors."""
    from .passhash import hash_password, is_supported_hash

    given_hash = args.password_hash if args.password_hash is not None else _env("WCMUX_PASSWORD_HASH")
    given_plain = args.password if args.password is not None else _env("WCMUX_PASSWORD")

    if given_hash:
        if not is_supported_hash(given_hash):
            sys.stderr.write(
                "error: unsupported password hash format. Expected a prefix in "
                "$argon2id$ / $argon2i$ / $argon2d$ / $2a$ / $2b$ / $2y$.\n"
            )
            sys.exit(2)
        if given_plain:
            sys.stderr.write(
                "warning: plaintext password ignored because --password-hash is set.\n"
            )
        return given_hash

    if given_plain:
        sys.stderr.write(
            "warning: plaintext password accepted; consider providing "
            "--password-hash instead (see `wcmux hash-password`).\n"
        )
        derived = hash_password(given_plain)
        # Drop references to the plaintext as best Python allows.
        args.password = None
        for k in ("WCMUX_PASSWORD",):
            if k in os.environ:
                # Overwrite before delete to reduce the window it sits in environ pages.
                os.environ[k] = ""
                del os.environ[k]
        given_plain = None
        return derived

    sys.stderr.write(
        "error: no password configured. Set --password-hash / WCMUX_PASSWORD_HASH "
        "(preferred) or --password / WCMUX_PASSWORD.\n"
    )
    sys.exit(2)


def resolve_config(argv: Optional[list[str]] = None) -> Config:
    args = build_parser().parse_args(argv)
    password_hash = _resolve_password_hash(args)

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
        password_hash=password_hash,
        port=port,
        host=host,
        base_url=base_url,
        shell=shell,
        secret_key=secret_key,
        trust_proxy=bool(trust_proxy),
    )


def _hash_password_cmd(argv: Optional[list[str]]) -> int:
    """`wcmux hash-password` — read a password from stdin/tty and print an argon2id hash."""
    from .passhash import hash_password

    p = argparse.ArgumentParser(prog="wcmux hash-password",
                                description="Print an argon2id hash for a password.")
    p.parse_args(argv or [])

    if sys.stdin.isatty():
        try:
            pw = getpass.getpass("Password: ")
            pw2 = getpass.getpass("Confirm:  ")
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write("\naborted.\n")
            return 130
        if pw != pw2:
            sys.stderr.write("error: passwords do not match.\n")
            return 2
    else:
        pw = sys.stdin.readline().rstrip("\n")
    if not pw:
        sys.stderr.write("error: empty password.\n")
        return 2
    sys.stdout.write(hash_password(pw) + "\n")
    return 0


def main(argv: Optional[list[str]] = None) -> None:
    argv = list(argv) if argv is not None else sys.argv[1:]

    if argv and argv[0] == "hash-password":
        rc = _hash_password_cmd(argv[1:])
        sys.exit(rc)

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
