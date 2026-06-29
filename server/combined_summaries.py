from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .paths import combined_summaries_file


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
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {cid: CombinedSummary(**data) for cid, data in raw.items()}


def save_combined_summaries(summaries: dict[str, CombinedSummary]) -> None:
    path = combined_summaries_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {cid: asdict(rec) for cid, rec in summaries.items()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
