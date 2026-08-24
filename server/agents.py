from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .paths import agents_file
from .json_store import load_json_object, save_json_object


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
    raw = load_json_object(path, label="Agent 数据")
    records: dict[str, AgentRecord] = {}
    for aid, data in raw.items():
        try:
            if not isinstance(data, dict):
                raise TypeError("Agent 记录必须是对象")
            item = dict(data)
            item.setdefault("enable_soul", False)
            item.setdefault("enable_rules", False)
            item.setdefault("enable_skills", False)
            item.setdefault("enable_memory", False)
            item.setdefault("rules_dir", "")
            item.setdefault("skills_dir", "")
            item.setdefault("memory_dir", "")
            records[aid] = AgentRecord(**item)
        except (TypeError, ValueError):
            continue
    return records


def save_agents(agents: dict[str, AgentRecord]) -> None:
    path = agents_file()
    payload = {aid: asdict(rec) for aid, rec in agents.items()}
    save_json_object(path, payload)
