from __future__ import annotations

import pytest

from KnowledgeBase import catalog


def test_catalog_adopts_legacy_default_as_editable_and_persists_created_base(tmp_path):
    catalog_path = tmp_path / "knowledge_bases.json"

    created = catalog.create_knowledge_base("市场资料", "竞品与市场研究", catalog_path)
    bases = catalog.list_knowledge_bases(catalog_path)

    assert bases[0].id == catalog.DEFAULT_KNOWLEDGE_BASE_ID
    assert bases[0].is_default is False
    assert bases[1] == created
    assert catalog.require_knowledge_base(created.id, catalog_path) == created


def test_catalog_rejects_duplicate_names_and_unknown_routes(tmp_path):
    catalog_path = tmp_path / "knowledge_bases.json"
    catalog.create_knowledge_base("指标口径", catalog_path=catalog_path)

    with pytest.raises(catalog.KnowledgeBaseCatalogError, match="已存在"):
        catalog.create_knowledge_base("指标口径", catalog_path=catalog_path)
    with pytest.raises(catalog.KnowledgeBaseCatalogError, match="不存在"):
        catalog.require_knowledge_base("missing", catalog_path)


def test_catalog_resolves_multiple_routes_without_duplicates(tmp_path):
    catalog_path = tmp_path / "knowledge_bases.json"
    market = catalog.create_knowledge_base("市场资料", catalog_path=catalog_path)

    resolved = catalog.require_knowledge_bases(
        [catalog.DEFAULT_KNOWLEDGE_BASE_ID, market.id, market.id],
        catalog_path,
    )

    assert [item.id for item in resolved] == ["default", market.id]


def test_catalog_can_update_and_delete_legacy_default_base(tmp_path):
    catalog_path = tmp_path / "knowledge_bases.json"

    updated = catalog.update_knowledge_base(
        catalog.DEFAULT_KNOWLEDGE_BASE_ID,
        "通用资料",
        "重命名后的旧资料库",
        catalog_path,
    )
    deleted = catalog.delete_knowledge_base(updated.id, catalog_path)

    assert updated.name == "通用资料"
    assert deleted == updated
    assert catalog.list_knowledge_bases(catalog_path) == []
