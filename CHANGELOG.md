# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-12

Initial public release.

### Core SDK
- `init(api_key=...)`, `capture_exception()`, `log()`, `close()`, `flush()`.
- Background flush thread with batched HTTP transport (zero runtime dependencies; stdlib `urllib`).
- Configuration via env vars (`ERRSIGHT_API_KEY`, `ERRSIGHT_ENV`, `ERRSIGHT_HOST`, `ERRSIGHT_RELEASE`) or `init(**kwargs)`.
- 429 backoff with `Retry-After` (capped at 600s), payload split at 490KB.
- Fork-safety via `os.register_at_fork` — events not silently dropped under gunicorn/uWSGI cluster mode.
- `before_send` hook with pass-through-on-exception semantics.

### Scope and Hub
- `Scope` with user, tags, and dual-ring breadcrumbs (50 manual / 30 DB).
- ContextVar-backed scope stack — concurrent async requests on a single event-loop thread are isolated.
- `with_scope()` works as both `with` and `async with`.
- `Scope.to_dict()` / `Scope.from_dict()` for cross-process propagation (Celery, RQ).

### Exception details
- Structured stack frames via `traceback.extract_tb`, capped at 50 frames.
- In-app frame heuristic excludes stdlib, `site-packages`, and `<frozen>` / `<string>` paths.
- `±5` lines of source context attached to in-app frames via `linecache`, byte-bounded at 256 per line.
- Cause chain walking (`__cause__` → `__context__` with `__suppress_context__` honored), capped at depth 5 with cycle protection.

### Integrations
- **`errsight.LoggingHandler`** — `logging.Handler` subclass; `logger.exception(...)` routes through `capture_exception`. `extra={"errsight": {"user": ..., "tags": ..., "fingerprint": ...}}` override channel.
- **`errsight.django.ErrsightMiddleware`** — sync and async middleware (`sync_capable`/`async_capable`). Per-request scope, `request.user` extraction, `process_exception` capture.
- **`errsight.flask.ErrsightFlask`** — Flask extension. `flask_login.current_user` + `g.user` support. `got_request_exception` signal for capture. `HTTPException` filtered (404 isn't an error).
- **`errsight.starlette.ErrsightMiddleware`** (re-exported as `errsight.fastapi.ErrsightMiddleware`) — pure ASGI middleware. Late-binds user from `scope["user"]` and route name from `scope["endpoint"]` / `app.routes`.
- **`errsight.celery`** — `install()` / `uninstall()` wires `before_task_publish` (scope → headers), `task_prerun` / `task_postrun` (scope push/pop), `task_failure` (capture).
- **`errsight.rq`** — `ErrsightWorker` (forking) and `ErrsightSimpleWorker` (non-forking) auto-register the exception handler and sync-flush after each `perform_job`. `register_handler(worker)` for attaching to an existing Worker.
- **`errsight.aws_lambda.errsight_lambda`** — decorator with per-invocation scope, context metadata (function name, version, request id, remaining time), and sync flush before return.

### Tests
- 134 tests across 13 test files; mypy `--strict` clean across all 16 source modules.

[0.1.0]: https://github.com/errsight/errsight-python/releases/tag/v0.1.0
