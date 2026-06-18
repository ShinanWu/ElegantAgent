from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .paths import discussions_file


@dataclass
class Discussion:
    id: str
    agent_id: str
    anchor: dict[str, Any]
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    collapsed: bool = False

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agentId": self.agent_id,
            "anchor": self.anchor,
            "messages": self.messages,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "collapsed": self.collapsed,
        }


def new_discussion(agent_id: str, anchor: dict[str, Any]) -> Discussion:
    return Discussion(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        anchor=anchor,
    )


def load_discussions() -> dict[str, Discussion]:
    path = discussions_file()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {did: Discussion(**data) for did, data in raw.items()}


def save_discussions(discussions: dict[str, Discussion]) -> None:
    path = discussions_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {did: asdict(rec) for did, rec in discussions.items()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
