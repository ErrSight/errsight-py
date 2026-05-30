from __future__ import annotations

from contextvars import ContextVar, Token
from types import TracebackType
from typing import Optional, Tuple, Type

from errsight.scope import Scope

# A ContextVar (not threading.local) is load-bearing here: asyncio runs many
# coroutines on a single thread. threading.local would silently leak state
# across concurrent tasks (FastAPI/Starlette/Celery async tasks share a
# thread). ContextVar values are copied at asyncio.create_task time and
# isolated per task, so each request handler sees its own scope stack.
_scope_stack: ContextVar[Tuple[Scope, ...]] = ContextVar("errsight_scope_stack")


def _get_stack() -> Tuple[Scope, ...]:
    stack = _scope_stack.get(())
    if not stack:
        stack = (Scope(),)
        _scope_stack.set(stack)
    return stack


def current_scope() -> Scope:
    return _get_stack()[-1]


def reset() -> None:
    """Clear the scope stack to a fresh root. Public-ish for test setup."""
    _scope_stack.set((Scope(),))


def push_scope(
    custom: Optional[Scope] = None,
) -> Tuple[Scope, Token[Tuple[Scope, ...]]]:
    """Push a new scope onto the stack. Returns ``(scope, token)``.

    Lower-level than :func:`with_scope` for framework integrations whose
    setup and teardown live in separate callbacks (e.g. Flask's
    ``before_request`` / ``teardown_request``). Always pair with
    :func:`pop_scope` in a try/finally — orphaning a push leaks an entry
    on the ContextVar stack for the rest of the task.
    """
    stack = _get_stack()
    base = custom if custom is not None else stack[-1].copy()
    token = _scope_stack.set(stack + (base,))
    return base, token


def pop_scope(token: Token[Tuple[Scope, ...]]) -> None:
    """Pop the scope identified by ``token``. A token from a different
    context, or one already consumed, is silently ignored so callers
    don't have to track whether the corresponding push happened.
    """
    try:
        _scope_stack.reset(token)
    except (ValueError, LookupError):
        pass


class _ScopeContextManager:
    __slots__ = ("_custom", "_scope", "_token")

    def __init__(self, custom: Optional[Scope]) -> None:
        self._custom = custom
        self._scope: Optional[Scope] = None
        self._token: Optional[Token[Tuple[Scope, ...]]] = None

    def _enter(self) -> Scope:
        stack = _get_stack()
        base = self._custom if self._custom is not None else stack[-1].copy()
        self._scope = base
        # set() returns a Token bound to this context; reset() in the same
        # context restores the previous value. Tuples (vs. lists) make the
        # snapshot semantics explicit — never mutate the existing value.
        self._token = _scope_stack.set(stack + (base,))
        return base

    def _exit(self) -> None:
        token = self._token
        self._token = None
        if token is None:
            return
        try:
            _scope_stack.reset(token)
        except (ValueError, LookupError):
            # Token from a different context, or stack already cleared.
            # Either is non-fatal; swallow rather than mask the in-flight
            # exception that triggered the __exit__.
            pass

    def __enter__(self) -> Scope:
        return self._enter()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self._exit()

    async def __aenter__(self) -> Scope:
        return self._enter()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self._exit()


def with_scope(scope: Optional[Scope] = None) -> _ScopeContextManager:
    return _ScopeContextManager(scope)
