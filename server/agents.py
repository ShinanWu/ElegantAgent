from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .paths import agents_file


@dataclass
class AgentRecord:
    id: str
    name: str
    cwd: str
    model: str
    sdk_agent_id: str | None = None
    enable_soul: bool = False
    enable_rules: bool = False
    enable_skills: bool = False
    enable_memory: bool = False
    rules_dir: str = ""
    skills_dir: str = ""
    memory_dir: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_summary(self, *, running: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cwd": self.cwd,
            "model": self.model,
            "enableSoul": self.enable_soul,
            "enableRules": self.enable_rules,
            "enableSkills": self.enable_skills,
            "enableMemory": self.enable_memory,
            "rulesDir": self.rules_dir,
            "skillsDir": self.skills_dir,
            "memoryDir": self.memory_dir,
            "updatedAt": self.updated_at,
            "messageCount": len(self.messages),
            "running": running,
        }

    def to_detail(self, *, running: bool = False) -> dict[str, Any]:
        data = self.to_summary(running=running)
        data["messages"] = self.messages
        return data


def new_agent(
    name: str,
    cwd: str,
    model: str,
) -> AgentRecord:
    return AgentRecord(
        id=str(uuid.uuid4()),
        name=name.strip() or "新 Agent",
        cwd=cwd,
        model=model,
    )


def load_agents() -> dict[str, AgentRecord]:
    path = agents_file()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    records: dict[str, AgentRecord] = {}
    for aid, data in raw.items():
        data.setdefault("enable_soul", False)
        data.setdefault("enable_rules", False)
        data.setdefault("enable_skills", False)
        data.setdefault("enable_memory", False)
        data.setdefault("rules_dir", "")
        data.setdefault("skills_dir", "")
        data.setdefault("memory_dir", "")
        records[aid] = AgentRecord(**data)
    return records


def save_agents(agents: dict[str, AgentRecord]) -> None:
    path = agents_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {aid: asdict(rec) for aid, rec in agents.items()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
