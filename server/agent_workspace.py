from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from .agents import AgentRecord

SOUL_PLACEHOLDER = "在此用自然语言描述这个 Agent 的身份、职责、权限与行为方式。"

SOURCE_KINDS = ("rules", "skills", "memory")


def agent_dir(cwd: str | Path) -> Path:
    return Path(cwd).expanduser().resolve() / ".agent"


def workspace_root(record: AgentRecord) -> Path:
    """Agent 工程目录（配置里的 cwd）。"""
    return Path(record.cwd).expanduser().resolve()


def scaffold_agent_dir(cwd: str | Path) -> Path:
    root = agent_dir(cwd)
    for sub in ("rules", "skills", "memory", "uploads", "outputs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    soul = root / "soul.md"
    if not soul.exists():
        soul.write_text(SOUL_PLACEHOLDER + "\n", encoding="utf-8")
    return root


def default_rules_dir(record: AgentRecord) -> Path:
    return agent_dir(record.cwd) / "rules"


def default_skills_dir(record: AgentRecord) -> Path:
    return agent_dir(record.cwd) / "skills"


def default_memory_dir(record: AgentRecord) -> Path:
    return agent_dir(record.cwd) / "memory"


def resolve_rules_dir(record: AgentRecord, override: str | None = None) -> Path:
    raw = record.rules_dir if override is None else override
    if str(raw or "").strip():
        return Path(str(raw)).expanduser().resolve()
    return default_rules_dir(record)


def resolve_skills_dir(record: AgentRecord, override: str | None = None) -> Path:
    raw = record.skills_dir if override is None else override
    if str(raw or "").strip():
        return Path(str(raw)).expanduser().resolve()
    return default_skills_dir(record)


def resolve_memory_dir(record: AgentRecord, override: str | None = None) -> Path:
    raw = record.memory_dir if override is None else override
    if str(raw or "").strip():
        return Path(str(raw)).expanduser().resolve()
    return default_memory_dir(record)


def read_soul(cwd: str | Path) -> str:
    path = agent_dir(cwd) / "soul.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def write_soul(cwd: str | Path, content: str) -> None:
    root = scaffold_agent_dir(cwd)
    (root / "soul.md").write_text(content, encoding="utf-8")


def _safe_rel_path(rel: str) -> Path:
    rel = rel.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        raise ValueError("非法路径")
    return Path(rel)


def _safe_path_under(base: Path, rel: str) -> Path:
    root = base.expanduser().resolve()
    target = (root / _safe_rel_path(rel)).resolve()
    if root != target and root not in target.parents:
        raise ValueError("非法路径")
    return target


def resolve_attachment_path(record: AgentRecord, item: dict[str, Any]) -> Path | None:
    """用户引用的附件路径（可为工程外绝对路径）。"""
    raw = item.get("path") or item.get("relative") or item.get("name")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = workspace_root(record) / path
    try:
        path = path.resolve()
    except OSError:
        return None
    if path.is_file() or path.is_dir():
        return path
    return None


def _collect_md_files(folder: Path, limit: int = 12) -> list[dict[str, str]]:
    if not folder.is_dir():
        return []
    items: list[dict[str, str]] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".mdc", ".txt"}:
            continue
        rel = path.relative_to(folder).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        items.append({"path": rel, "content": text[:4000]})
        if len(items) >= limit:
            break
    return items


def _collect_skill_files(folder: Path, limit: int = 12) -> list[dict[str, str]]:
    if not folder.is_dir():
        return []
    items: list[dict[str, str]] = []
    for skill_dir in sorted(folder.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_file():
            items.append(
                {
                    "path": f"{skill_dir.name}/SKILL.md",
                    "content": skill_file.read_text(encoding="utf-8")[:4000],
                }
            )
    if items:
        return items[:limit]
    return _collect_md_files(folder, limit)


def collect_rules(record: AgentRecord) -> list[dict[str, str]]:
    return _collect_md_files(resolve_rules_dir(record))


def collect_skills(record: AgentRecord) -> list[dict[str, str]]:
    return _collect_skill_files(resolve_skills_dir(record))


def collect_memory(record: AgentRecord) -> list[dict[str, str]]:
    return _collect_md_files(resolve_memory_dir(record))


def walk_dir_tree(base: Path) -> list[dict[str, Any]]:
    def walk(folder: Path, prefix: str) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        if not folder.is_dir():
            return nodes
        for child in sorted(folder.iterdir()):
            if child.name.startswith("."):
                continue
            rel = f"{prefix}{child.name}" if prefix else child.name
            if child.is_dir():
                nodes.append({"type": "dir", "path": rel, "children": walk(child, rel + "/")})
            elif child.suffix.lower() in {".md", ".mdc", ".txt"}:
                nodes.append({"type": "file", "path": rel})
        return nodes

    return walk(base.expanduser().resolve(), "")


def list_agent_config(record: AgentRecord, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    overrides = overrides or {}
    scaffold_agent_dir(record.cwd)

    def pick(key: str) -> str | None:
        return overrides[key] if key in overrides else None

    rules_path = resolve_rules_dir(record, pick("rulesDir"))
    skills_path = resolve_skills_dir(record, pick("skillsDir"))
    memory_path = resolve_memory_dir(record, pick("memoryDir"))

    return {
        "soul": read_soul(record.cwd),
        "rulesDir": str(rules_path),
        "skillsDir": str(skills_path),
        "memoryDir": str(memory_path),
        "defaultRulesDir": str(default_rules_dir(record)),
        "defaultSkillsDir": str(default_skills_dir(record)),
        "defaultMemoryDir": str(default_memory_dir(record)),
        "rulesTree": walk_dir_tree(rules_path),
        "skillsTree": walk_dir_tree(skills_path),
        "memoryTree": walk_dir_tree(memory_path),
    }


def read_config_file(record: AgentRecord, source: str, rel_path: str) -> str:
    if source == "soul":
        return read_soul(record.cwd)
    if source not in SOURCE_KINDS:
        raise ValueError("未知配置来源")
    resolver = {
        "rules": resolve_rules_dir,
        "skills": resolve_skills_dir,
        "memory": resolve_memory_dir,
    }[source]
    path = _safe_path_under(resolver(record), rel_path)
    if not path.is_file():
        raise FileNotFoundError(rel_path)
    return path.read_text(encoding="utf-8")


def write_config_file(record: AgentRecord, source: str, rel_path: str, content: str) -> None:
    if source == "soul":
        write_soul(record.cwd, content)
        return
    if source not in SOURCE_KINDS:
        raise ValueError("未知配置来源")
    resolver = {
        "rules": resolve_rules_dir,
        "skills": resolve_skills_dir,
        "memory": resolve_memory_dir,
    }[source]
    base = resolver(record)
    base.mkdir(parents=True, exist_ok=True)
    path = _safe_path_under(base, rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def save_upload(
    cwd: str | Path,
    filename: str,
    data: bytes,
) -> dict[str, str]:
    root = scaffold_agent_dir(cwd)
    uploads = root / "uploads"
    safe_name = re.sub(r"[^\w.\-]+", "_", Path(filename).name) or "file"
    dest = uploads / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    dest.write_bytes(data)
    return {
        "name": dest.name,
        "path": str(dest),
        "relative": f".agent/uploads/{dest.name}",
    }
