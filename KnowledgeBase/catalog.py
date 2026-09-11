"""Persistent catalog for routing documents and retrieval to knowledge bases."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4


KNOWLEDGE_ROOT = Path(__file__).resolve().parent
CATALOG_PATH = KNOWLEDGE_ROOT / "knowledge_bases.json"
DEFAULT_KNOWLEDGE_BASE_ID = "default"
DEFAULT_KNOWLEDGE_BASE_NAME = "默认知识库"
_CATALOG_LOCK = Lock()


class KnowledgeBaseCatalogError(ValueError):
    """Raised when a knowledge-base catalog operation is invalid."""


@dataclass(frozen=True)
class KnowledgeBaseRecord:
    """One selectable knowledge base."""

    id: str
    name: str
    description: str
    created_at: str
    is_default: bool = False


def list_knowledge_bases(catalog_path: Path = CATALOG_PATH) -> list[KnowledgeBaseRecord]:
    """Return every persisted knowledge base, including the legacy default route."""
    legacy_default = _legacy_default_record()
    if not catalog_path.exists():
        return [legacy_default]
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [legacy_default]

    if isinstance(payload, dict) and isinstance(payload.get("bases"), list):
        items = payload["bases"]
    elif isinstance(payload, list):
        # Catalogs created before editable bases omitted the implicit default base.
        items = [asdict(legacy_default), *payload]
    else:
        return [legacy_default]

    records: list[KnowledgeBaseRecord] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            record = KnowledgeBaseRecord(
                id=str(item["id"]),
                name=str(item["name"]).strip(),
                description=str(item.get("description") or "").strip(),
                created_at=str(item["created_at"]),
                is_default=False,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not record.name or record.id in seen:
            continue
        seen.add(record.id)
        records.append(record)
    return records


def get_knowledge_base(
    knowledge_base_id: str,
    catalog_path: Path = CATALOG_PATH,
) -> KnowledgeBaseRecord | None:
    """Return one knowledge base by its stable identifier."""
    normalized_id = (knowledge_base_id or "").strip()
    return next(
        (item for item in list_knowledge_bases(catalog_path) if item.id == normalized_id),
        None,
    )


def require_knowledge_base(
    knowledge_base_id: str | None,
    catalog_path: Path = CATALOG_PATH,
) -> KnowledgeBaseRecord:
    """Resolve a requested base, using the default only when no id is supplied."""
    normalized_id = (
        knowledge_base_id.strip()
        if isinstance(knowledge_base_id, str) and knowledge_base_id.strip()
        else DEFAULT_KNOWLEDGE_BASE_ID
    )
    record = get_knowledge_base(normalized_id, catalog_path)
    if record is None:
        raise KnowledgeBaseCatalogError("目标知识库不存在")
    return record


def require_knowledge_bases(
    knowledge_base_ids: list[str] | None,
    catalog_path: Path = CATALOG_PATH,
) -> list[KnowledgeBaseRecord]:
    """Resolve one or more requested bases while preserving selection order."""
    requested_ids = knowledge_base_ids if isinstance(knowledge_base_ids, list) else []
    normalized_ids = list(dict.fromkeys(
        item.strip() for item in requested_ids if isinstance(item, str) and item.strip()
    ))
    if not normalized_ids:
        normalized_ids = [DEFAULT_KNOWLEDGE_BASE_ID]
    records = list_knowledge_bases(catalog_path)
    by_id = {record.id: record for record in records}
    if any(item not in by_id for item in normalized_ids):
        raise KnowledgeBaseCatalogError("目标知识库不存在")
    return [by_id[item] for item in normalized_ids]


def create_knowledge_base(
    name: str,
    description: str = "",
    catalog_path: Path = CATALOG_PATH,
) -> KnowledgeBaseRecord:
    """Create a selectable knowledge base with a permanent UUID."""
    normalized_name = (name or "").strip()
    normalized_description = (description or "").strip()
    if not normalized_name:
        raise KnowledgeBaseCatalogError("知识库名称不能为空")
    if len(normalized_name) > 80:
        raise KnowledgeBaseCatalogError("知识库名称不能超过 80 个字符")

    with _CATALOG_LOCK:
        existing = list_knowledge_bases(catalog_path)
        if any(item.name.casefold() == normalized_name.casefold() for item in existing):
            raise KnowledgeBaseCatalogError("知识库名称已存在")
        record = KnowledgeBaseRecord(
            id=str(uuid4()),
            name=normalized_name,
            description=normalized_description[:300],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        _write_catalog([*existing, record], catalog_path)
    return record


def update_knowledge_base(
    knowledge_base_id: str,
    name: str,
    description: str = "",
    catalog_path: Path = CATALOG_PATH,
) -> KnowledgeBaseRecord:
    """Update the editable name and description of one knowledge base."""
    normalized_name = (name or "").strip()
    normalized_description = (description or "").strip()
    if not normalized_name:
        raise KnowledgeBaseCatalogError("知识库名称不能为空")
    if len(normalized_name) > 80:
        raise KnowledgeBaseCatalogError("知识库名称不能超过 80 个字符")

    with _CATALOG_LOCK:
        records = list_knowledge_bases(catalog_path)
        current = next((item for item in records if item.id == knowledge_base_id), None)
        if current is None:
            raise KnowledgeBaseCatalogError("目标知识库不存在")
        if any(
            item.id != knowledge_base_id
            and item.name.casefold() == normalized_name.casefold()
            for item in records
        ):
            raise KnowledgeBaseCatalogError("知识库名称已存在")
        updated = replace(
            current,
            name=normalized_name,
            description=normalized_description[:300],
            is_default=False,
        )
        _write_catalog(
            [updated if item.id == knowledge_base_id else item for item in records],
            catalog_path,
        )
    return updated


def delete_knowledge_base(
    knowledge_base_id: str,
    catalog_path: Path = CATALOG_PATH,
) -> KnowledgeBaseRecord:
    """Remove one knowledge base from the catalog without protecting any ID."""
    with _CATALOG_LOCK:
        records = list_knowledge_bases(catalog_path)
        current = next((item for item in records if item.id == knowledge_base_id), None)
        if current is None:
            raise KnowledgeBaseCatalogError("目标知识库不存在")
        _write_catalog(
            [item for item in records if item.id != knowledge_base_id],
            catalog_path,
        )
    return current


def _legacy_default_record() -> KnowledgeBaseRecord:
    """Return the editable catalog entry used to adopt legacy unassigned documents."""
    return KnowledgeBaseRecord(
        id=DEFAULT_KNOWLEDGE_BASE_ID,
        name=DEFAULT_KNOWLEDGE_BASE_NAME,
        description="兼容已有文档的知识库",
        created_at="",
        is_default=False,
    )


def _write_catalog(records: list[KnowledgeBaseRecord], catalog_path: Path) -> None:
    """Persist the current catalog in the editable version-two format."""
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = catalog_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(
            {"version": 2, "bases": [asdict(item) for item in records]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(catalog_path)
