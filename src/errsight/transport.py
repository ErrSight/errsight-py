from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from errsight.configuration import Configuration
from errsight.version import __version__

MAX_PAYLOAD_BYTES = 490 * 1024
MAX_RATE_LIMIT_SECONDS = 600


class Transport:
    _fork_hook_registered: bool = False
    _current: "Optional[Transport]" = None

    def __init__(self, config: Configuration) -> None:
        self._config = config
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(
            maxsize=config.max_queue_size
        )
        self._shutdown = threading.Event()
        self._rate_limited_until: float = 0.0
        self._pid = os.getpid()
        self._thread: Optional[threading.Thread] = None
        self._start_worker()
        Transport._current = self
        self._ensure_fork_hook()

    def _start_worker(self) -> None:
        thread = threading.Thread(
            target=self._run, name="errsight-flush", daemon=True
        )
        thread.start()
        self._thread = thread

    @classmethod
    def _ensure_fork_hook(cls) -> None:
        if cls._fork_hook_registered:
            return
        register = getattr(os, "register_at_fork", None)
        if register is None:
            return
        try:
            register(after_in_child=cls._on_fork_in_child)
            cls._fork_hook_registered = True
        except RuntimeError:
            pass

    @classmethod
    def _on_fork_in_child(cls) -> None:
        current = cls._current
        if current is not None:
            current._after_fork()

    def _after_fork(self) -> None:
        # POSIX fork drops all threads except the caller; rebuild state so
        # the child has a working flush worker and uninherited primitives.
        self._queue = queue.Queue(maxsize=self._config.max_queue_size)
        self._shutdown = threading.Event()
        self._rate_limited_until = 0.0
        self._pid = os.getpid()
        self._thread = None
        self._start_worker()

    def enqueue(self, event: Dict[str, Any]) -> None:
        if self._pid != os.getpid():
            self._after_fork()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            sys.stderr.write(
                f"[errsight] queue full (max {self._config.max_queue_size}), dropping event\n"
            )

    def _run(self) -> None:
        while not self._shutdown.is_set():
            self._shutdown.wait(self._config.flush_interval)
            self._flush()
        # Best-effort final drain. close()'s thread.join(shutdown_timeout)
        # bounds total time, so the loop can't hang the host process.
        while not self._queue.empty():
            if time.monotonic() < self._rate_limited_until:
                break
            before = self._queue.qsize()
            self._flush()
            if self._queue.qsize() >= before:
                break  # no progress — bail rather than spin

    def _flush(self) -> None:
        if time.monotonic() < self._rate_limited_until:
            return
        batch: List[Dict[str, Any]] = []
        while len(batch) < self._config.batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._send(batch)

    def _send(self, events: List[Dict[str, Any]]) -> None:
        body = json.dumps(events, default=str, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_PAYLOAD_BYTES and len(events) > 1:
            mid = (len(events) + 1) // 2
            self._send(events[:mid])
            self._send(events[mid:])
            return

        request = urllib.request.Request(
            self._config.events_endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self._config.api_key or "",
                "User-Agent": f"errsight-py/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout) as resp:
                # 200/202 are success; we don't care about the body.
                resp.read(0)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after_raw = exc.headers.get("Retry-After", "60") if exc.headers else "60"
                try:
                    retry_after = int(retry_after_raw)
                except (TypeError, ValueError):
                    retry_after = 60
                retry_after = min(max(retry_after, 1), MAX_RATE_LIMIT_SECONDS)
                self._rate_limited_until = time.monotonic() + retry_after
                # Put events back; flush worker will skip sends while paused.
                for ev in events:
                    try:
                        self._queue.put_nowait(ev)
                    except queue.Full:
                        break
                sys.stderr.write(
                    f"[errsight] rate-limited; pausing sends for {retry_after}s\n"
                )
            else:
                sys.stderr.write(
                    f"[errsight] API error {exc.code}: {exc.reason}\n"
                )
        except (urllib.error.URLError, OSError) as exc:
            sys.stderr.write(f"[errsight] send failed: {exc}\n")

    def flush(self, timeout: float = 5.0) -> bool:
        """Drain queued events synchronously without stopping the worker.

        Useful when the host process can be frozen between invocations
        (AWS Lambda, Google Cloud Functions) and the background flush
        thread isn't guaranteed to run before the next freeze.

        Returns ``True`` if the queue was emptied within ``timeout``,
        ``False`` otherwise (timeout exceeded, rate-limited, or no
        progress on consecutive flushes).
        """
        deadline = time.monotonic() + timeout
        while not self._queue.empty():
            now = time.monotonic()
            if now >= deadline:
                return False
            if now < self._rate_limited_until:
                wait = min(0.1, max(0.0, deadline - now))
                if wait <= 0.0:
                    return False
                # Short sleep so we don't busy-spin during rate-limit pause;
                # caller's overall deadline still bounds us.
                self._shutdown.wait(wait)
                continue
            before = self._queue.qsize()
            self._flush()
            if self._queue.qsize() >= before:
                # _flush made no progress (all sends failed, or the queue
                # was modified concurrently in a way we can't drain). Bail
                # rather than spin to the deadline.
                return False
        return True

    def close(self) -> None:
        self._shutdown.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(self._config.shutdown_timeout)
