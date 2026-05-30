from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

MAX_USER_BREADCRUMBS = 50
MAX_DB_BREADCRUMBS = 30


@dataclass
class Scope:
    """Holds the user, tags, and breadcrumbs attached to events captured
    while this scope is on top of the hub stack.

    Breadcrumbs are split into two ring buffers — manual app crumbs and
    auto-collected DB crumbs — so a high-query request can't evict the
    user's manual context. The public `breadcrumbs` property returns a
    merged, timestamp-sorted view.
    """

    user: Optional[Dict[str, Any]] = None
    tags: Dict[str, str] = field(default_factory=dict)
    user_breadcrumbs: List[Dict[str, Any]] = field(default_factory=list)
    db_breadcrumbs: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def breadcrumbs(self) -> List[Dict[str, Any]]:
        if not self.db_breadcrumbs:
            return list(self.user_breadcrumbs)
        if not self.user_breadcrumbs:
            return list(self.db_breadcrumbs)
        return sorted(
            self.user_breadcrumbs + self.db_breadcrumbs,
            key=lambda c: str(c.get("timestamp", "")),
        )

    def set_user(self, user: Optional[Mapping[str, Any]]) -> None:
        self.user = dict(user) if isinstance(user, Mapping) else None

    def clear_user(self) -> None:
        self.user = None

    def set_tag(self, key: Any, value: Any) -> None:
        if key is None:
            return
        self.tags[str(key)] = str(value)

    def set_tags(self, tags: Optional[Mapping[str, Any]]) -> None:
        if not isinstance(tags, Mapping):
            return
        for k, v in tags.items():
            self.set_tag(k, v)

    def clear_tags(self) -> None:
        self.tags = {}

    def add_breadcrumb(
        self,
        *,
        category: str,
        message: str,
        level: str = "info",
        data: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.user_breadcrumbs.append(self._build_crumb(category, message, level, data))
        while len(self.user_breadcrumbs) > MAX_USER_BREADCRUMBS:
            self.user_breadcrumbs.pop(0)

    def add_db_breadcrumb(
        self,
        *,
        message: str,
        data: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.db_breadcrumbs.append(self._build_crumb("db", message, "info", data))
        while len(self.db_breadcrumbs) > MAX_DB_BREADCRUMBS:
            self.db_breadcrumbs.pop(0)

    def clear_breadcrumbs(self) -> None:
        self.user_breadcrumbs = []
        self.db_breadcrumbs = []

    def copy(self) -> "Scope":
        new = Scope()
        new.user = dict(self.user) if self.user else None
        new.tags = dict(self.tags)
        new.user_breadcrumbs = [dict(c) for c in self.user_breadcrumbs]
        new.db_breadcrumbs = [dict(c) for c in self.db_breadcrumbs]
        return new

    def merge(self, other: "Scope") -> "Scope":
        """Overlay another scope's state. `other` wins on user; tags merge
        with `other` taking precedence on key collisions; breadcrumbs are
        appended and clipped to the per-buffer caps.
        """
        if not isinstance(other, Scope):
            return self
        if other.user:
            self.user = dict(other.user)
        if other.tags:
            self.tags.update(other.tags)
        if other.user_breadcrumbs:
            self.user_breadcrumbs.extend([dict(c) for c in other.user_breadcrumbs])
            while len(self.user_breadcrumbs) > MAX_USER_BREADCRUMBS:
                self.user_breadcrumbs.pop(0)
        if other.db_breadcrumbs:
            self.db_breadcrumbs.extend([dict(c) for c in other.db_breadcrumbs])
            while len(self.db_breadcrumbs) > MAX_DB_BREADCRUMBS:
                self.db_breadcrumbs.pop(0)
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for cross-process propagation (e.g. Celery payloads).

        DB breadcrumbs are intentionally not propagated — the receiving
        worker collects its own from its own queries; mixing parent and
        worker DB events would confuse debugging.
        """
        out: Dict[str, Any] = {}
        if self.user:
            out["user"] = dict(self.user)
        if self.tags:
            out["tags"] = dict(self.tags)
        if self.user_breadcrumbs:
            out["breadcrumbs"] = [dict(c) for c in self.user_breadcrumbs]
        return out

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Scope":
        scope = cls()
        if not isinstance(data, Mapping):
            return scope
        user = data.get("user")
        if isinstance(user, Mapping):
            scope.user = dict(user)
        tags = data.get("tags")
        if isinstance(tags, Mapping):
            scope.tags = {str(k): str(v) for k, v in tags.items()}
        crumbs = data.get("breadcrumbs")
        if isinstance(crumbs, list):
            scope.user_breadcrumbs = [
                dict(c) for c in crumbs if isinstance(c, Mapping)
            ]
        return scope

    @staticmethod
    def _build_crumb(
        category: str,
        message: str,
        level: str,
        data: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        if ts.endswith("+00:00"):
            ts = ts[:-6] + "Z"
        crumb: Dict[str, Any] = {
            "timestamp": ts,
            "category": str(category),
            "level": str(level),
            "message": str(message),
        }
        if isinstance(data, Mapping):
            crumb["data"] = dict(data)
        return crumb
