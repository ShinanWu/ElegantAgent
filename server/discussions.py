from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .paths import discussions_file
from .json_store import load_json_object, save_json_object


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
    summary: str | None = None
    summary_updated_at: str | None = None

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
            "summary": self.summary,
            "summaryUpdatedAt": self.summary_updated_at,
        }


def new_discussion(agent_id: str, anchor: dict[str, Any]) -> Discussion:
    return Discussion(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        anchor=anchor,
    )


def load_discussions() -> dict[str, Discussion]:
    path = discussions_file()
    raw = load_json_object(path, label="讨论数据")
    records: dict[str, Discussion] = {}
    for did, data in raw.items():
        try:
            if not isinstance(data, dict):
                raise TypeError("讨论记录必须是对象")
            records[did] = Discussion(**data)
        except (TypeError, ValueError):
            continue
    return records


def save_discussions(discussions: dict[str, Discussion]) -> None:
    path = discussions_file()
    payload = {did: asdict(rec) for did, rec in discussions.items()}
    save_json_object(path, payload)
