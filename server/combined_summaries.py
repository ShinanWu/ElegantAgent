from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .paths import combined_summaries_file
from .json_store import load_json_object, save_json_object


@dataclass
class CombinedSummary:
    id: str
    agent_id: str
    discussion_ids: list[str]
    summary: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agentId": self.agent_id,
            "discussionIds": self.discussion_ids,
            "summary": self.summary,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def new_combined_summary(agent_id: str, discussion_ids: list[str]) -> CombinedSummary:
    return CombinedSummary(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        discussion_ids=list(discussion_ids),
    )


def load_combined_summaries() -> dict[str, CombinedSummary]:
    path = combined_summaries_file()
    raw = load_json_object(path, label="合并总结数据")
    records: dict[str, CombinedSummary] = {}
    for cid, data in raw.items():
        try:
            if not isinstance(data, dict):
                raise TypeError("合并总结记录必须是对象")
            records[cid] = CombinedSummary(**data)
        except (TypeError, ValueError):
            continue
    return records


def save_combined_summaries(summaries: dict[str, CombinedSummary]) -> None:
    path = combined_summaries_file()
    payload = {cid: asdict(rec) for cid, rec in summaries.items()}
    save_json_object(path, payload)
