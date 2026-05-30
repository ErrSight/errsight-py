# errsight-py

Python SDK for [ErrSight](https://errsight.com) error tracking. Capture exceptions, attach context, and ship structured events from any Python app — sync or async — with first-class integrations for Django, Flask, FastAPI / Starlette, Celery, RQ, AWS Lambda, and the stdlib `logging` module.

- Zero runtime dependencies (stdlib `urllib` for transport)
- ContextVar-isolated scopes — concurrent async requests don't leak user/tag state
- Structured stack frames with ±5 lines of source context for in-app frames
- Python exception cause chain (`__cause__` / `__context__`) walked and shipped
- Background flush thread; fork-safe (`os.register_at_fork`); 429-aware

## Requirements

Python ≥ 3.8.

## Installation

```sh
pip install errsight                  # core SDK only
pip install 'errsight[django]'        # + Django
pip install 'errsight[flask]'         # + Flask
pip install 'errsight[fastapi]'       # + FastAPI / Starlette
pip install 'errsight[celery]'        # + Celery
pip install 'errsight[rq]'            # + RQ
```

## Quickstart

```python
import errsight

errsight.init(
    api_key="elp_...",         # required (or set ERRSIGHT_API_KEY)
    environment="production",  # default: ERRSIGHT_ENV or "production"
)

try:
    do_something_risky()
except Exception as exc:
    errsight.capture_exception(exc, metadata={"order_id": 42})
```

`errsight.init()` registers an `atexit` handler that drains the queue before the process exits — no manual `errsight.close()` needed.

## Configuration

```python
errsight.init(
    api_key=os.environ["ERRSIGHT_API_KEY"],
    environment="production",   # default: $ERRSIGHT_ENV or "production"
    host="https://errsight.com",# default: $ERRSIGHT_HOST or this
    release="v1.2.3",           # default: $ERRSIGHT_RELEASE
    min_level="warning",        # debug|info|warning|error|fatal
    batch_size=10,              # events per HTTP request
    flush_interval=2.0,         # seconds between background flushes
    max_queue_size=1_000,       # drop events beyond this
    timeout=5.0,                # HTTP timeout (seconds)
    shutdown_timeout=5.0,       # close()'s join timeout
    before_send=None,           # callable(event) -> event | None
    attach_to_logging=False,    # auto-attach logging.Handler to root
)
```

Environment variables read by the defaults: `ERRSIGHT_API_KEY`, `ERRSIGHT_ENV`, `ERRSIGHT_HOST`, `ERRSIGHT_RELEASE`.

### `before_send` hook

Final-mile event filter. Return the (possibly modified) event to send, or `None` to drop. Exceptions raised inside `before_send` are logged to stderr and the event passes through unmodified — silently dropping production errors because the customer's filter has a bug is worse than the bug itself.

```python
def scrub(event):
    event.get("metadata", {}).pop("credit_card", None)
    return event

errsight.init(api_key="...", before_send=scrub)
```

## Adding context to events

Scopes are per-request / per-task ContextVar values; mutations inside a `with errsight.with_scope():` block don't leak to other requests.

```python
errsight.set_user({"id": user.id, "email": user.email})
errsight.set_tag("region", "us-east")
errsight.set_tags({"plan": "pro", "shard": "5"})
errsight.add_breadcrumb(category="ui", message="clicked checkout", data={"cart_id": 42})

with errsight.with_scope():
    errsight.set_tag("transaction_id", txn.id)
    errsight.capture_exception(exc)   # tag only attached to this event
```

Async:

```python
async with errsight.with_scope():
    errsight.set_user({"id": user_id})
    await process_request()
```

## Log forwarding

`errsight.LoggingHandler` is a `logging.Handler` subclass. Records with `exc_info` (including `logger.exception(...)`) route through `capture_exception` so structured frames and the cause chain ship just like a direct capture call.

```python
import logging
import errsight

errsight.init(api_key="...")
logging.getLogger().addHandler(errsight.LoggingHandler(level=logging.WARNING))

logger = logging.getLogger("billing")
logger.error("payment failed", extra={
    "order_id": 42,                            # → metadata.order_id
    "errsight": {                              # → top-level event fields
        "user": {"id": "alice"},
        "tags": {"gateway": "stripe"},
        "fingerprint": "checkout-stripe",
    },
})
```

Or attach via configuration:

```python
errsight.init(api_key="...", attach_to_logging=True)
```

## Framework integrations

### Django

```python
# settings.py
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "errsight.django.ErrsightMiddleware",  # after AuthenticationMiddleware
    ...
]

# wsgi.py / asgi.py / AppConfig.ready()
import errsight
errsight.init(api_key=os.environ["ERRSIGHT_API_KEY"], environment=os.environ.get("DJANGO_ENV"))
```

Supports both sync and async views — the same class works in either mode (`sync_capable = True`, `async_capable = True`).

The middleware pushes a per-request scope tagged with `request_method`, `path`, `view`; populates user from `request.user` (works with `AbstractBaseUser` subclasses); and captures view exceptions with full request metadata (path, method, query params, view name).

### Flask

```python
from flask import Flask
import errsight
from errsight.flask import ErrsightFlask

errsight.init(api_key=os.environ["ERRSIGHT_API_KEY"])
app = Flask(__name__)
ErrsightFlask(app)
```

App-factory pattern:

```python
errsight_ext = ErrsightFlask()

def create_app():
    app = Flask(__name__)
    errsight_ext.init_app(app)
    return app
```

Reads user from `flask_login.current_user` if installed, otherwise from `g.user`. Register `ErrsightFlask` **after** any `before_request` hook that populates `g.user`.

### FastAPI / Starlette

```python
from fastapi import FastAPI
from errsight.fastapi import ErrsightMiddleware
import errsight

errsight.init(api_key=os.environ["ERRSIGHT_API_KEY"])
app = FastAPI()
app.add_middleware(ErrsightMiddleware)
```

(`errsight.starlette.ErrsightMiddleware` is the same class; FastAPI is built on Starlette.)

Pure ASGI — not `BaseHTTPMiddleware`, so it doesn't break streaming responses. Per-request scope is `contextvars`-isolated, so concurrent async requests on a single event-loop thread see their own user/tags. Late-binds `scope["user"]` from `AuthenticationMiddleware` (if installed) at capture time so the auth user is attached even though our middleware ran first.

### Celery

```python
import errsight
from errsight.celery import install

errsight.init(api_key=os.environ["ERRSIGHT_API_KEY"])
install()
```

Cross-process scope propagation: tasks enqueued from inside `with errsight.with_scope():` carry the publisher's user/tags/breadcrumbs through `task.request.headers["errsight"]` to the worker, which rehydrates them on `task_prerun`. Failures are captured on `task_failure` with task name, id, args, kwargs, and retry count.

### RQ (Redis Queue)

```python
from rq import Queue
from errsight.rq import ErrsightWorker
import errsight

errsight.init(api_key=os.environ["ERRSIGHT_API_KEY"])
worker = ErrsightWorker([Queue()])
worker.work()
```

Or attach the exception handler to an existing Worker without subclassing:

```python
from rq import Worker
from errsight.rq import register_handler

worker = Worker([Queue()])
register_handler(worker)
worker.work()
```

For cross-process scope propagation, pass it explicitly when enqueueing (RQ has no publisher-side signal):

```python
job = queue.enqueue(
    send_email, user.email,
    meta={"errsight": errsight.hub.current_scope().to_dict()},
)
```

`ErrsightSimpleWorker` is a non-forking variant (subclasses `rq.SimpleWorker`) for tests and inline development.

### AWS Lambda

```python
import errsight
from errsight.aws_lambda import errsight_lambda

errsight.init(api_key=os.environ["ERRSIGHT_API_KEY"])

@errsight_lambda
def lambda_handler(event, context):
    ...
```

Or with options:

```python
@errsight_lambda(flush_timeout=10.0, include_event=True)
def lambda_handler(event, context):
    ...
```

The decorator pushes a per-invocation scope tagged with `lambda_function`, `lambda_version`, `aws_request_id`; captures unhandled exceptions with the Lambda context (including `remaining_time_ms` — useful for diagnosing timeouts); and **synchronously flushes the transport before returning** so events aren't lost when Lambda freezes the process between invocations.

`include_event=True` ships a 4KB-truncated form of the event payload. Off by default — Lambda events frequently contain PII.

## How it works

Events are pushed onto a thread-safe in-memory queue. A background thread (`errsight-flush`) flushes them in batches of `batch_size` every `flush_interval` seconds (or sooner if the queue fills). The HTTP transport uses `urllib.request` with a per-batch connection; payload split happens at 490KB. On a 429 response the worker pauses sends until `Retry-After`, capped at 600s. On process exit `atexit` drains the queue with a 5s budget.

Fork-safety: `os.register_at_fork(after_in_child=...)` rebuilds the queue, locks, and worker thread in child processes — events captured under gunicorn/uWSGI cluster mode aren't silently dropped.

## License

MIT. See `LICENSE`.
