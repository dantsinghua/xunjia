def test_live_catalog_is_empty_until_oss_is_connected(tmp_path):
    """没配公网连接时，不能把内置样例工作台拿给页面选。"""
    from imlink.store import Store

    store = Store(tmp_path / "links.json")
    assert store.catalog_rows() == []
