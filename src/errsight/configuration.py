from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

EventDict = Dict[str, Any]
BeforeSend = Callable[[EventDict], Optional[EventDict]]


@dataclass
class Configuration:
    api_key: Optional[str] = field(
        default_factory=lambda: os.environ.get("ERRSIGHT_API_KEY")
    )
    environment: str = field(
        default_factory=lambda: os.environ.get("ERRSIGHT_ENV", "production")
    )
    host: str = field(
        default_factory=lambda: os.environ.get("ERRSIGHT_HOST", "https://errsight.com")
    )
    release: Optional[str] = field(
        default_factory=lambda: os.environ.get("ERRSIGHT_RELEASE")
    )
    min_level: str = "warning"
    timeout: float = 5.0
    batch_size: int = 10
    flush_interval: float = 2.0
    max_queue_size: int = 1000
    shutdown_timeout: float = 5.0
    enabled: bool = True
    before_send: Optional[BeforeSend] = None
    # Off by default. When True, init() attaches a LoggingHandler to the
    # root logger at WARNING. The reasoning mirrors the Ruby SDK's
    # attach_to_rails_logger=False default: broadcasting every log line
    # above min_level into ErrSight floods the issue list with framework
    # noise and burns customer event quota for things that belong in a log
    # aggregator, not an error tracker. Opt-in only.
    attach_to_logging: bool = False

    @property
    def events_endpoint(self) -> str:
        return f"{self.host.rstrip('/')}/api/v1/events"

    def is_enabled(self) -> bool:
        return self.enabled and bool(self.api_key and self.api_key.strip())
