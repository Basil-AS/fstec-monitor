from fstec_monitor.crawler import category_key, snapshot_required


def test_category_key_normalizes_nbsp_and_case():
    assert category_key("Информационные и аналитические материалы \xa0185") == "информационные и аналитические материалы 185"
    assert category_key("  Информационные   и аналитические материалы ") == "информационные и аналитические материалы"


def test_markup_only_change_does_not_require_archived_snapshot():
    assert not snapshot_required("same", "same", True)
    assert snapshot_required("old", "new", True)
    assert snapshot_required("", "new", False)
