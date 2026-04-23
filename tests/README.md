# wcmux tests

Integration tests that drive a running `wcmux` process. Each milestone gets its
own script.

## How to run

```bash
# Terminal 1: start the server
WCMUX_PASSWORD=pw .venv/bin/wcmux --port 8022 --host 127.0.0.1

# Terminal 2: run a milestone's tests
.venv/bin/python tests/test_m1_auth.py
.venv/bin/python tests/test_m2_single_tab.py
.venv/bin/python tests/test_m3_multitab.py
...
```

Test helper `tests/runserver.sh` starts + stops a server in one shot.

## Env vars

- `WCMUX_TEST_BASE` — base URL (default `http://127.0.0.1:8022`)
- `WCMUX_TEST_PASSWORD` — password (default `pw`)
