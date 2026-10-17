"""The removed demo service must not return as an alternate live entrypoint."""
from pathlib import Path


def test_mock_service_and_seed_entrypoints_are_removed():
    root=Path(__file__).resolve().parents[2]
    for name in ('mock/mock_server.py','mock/start.sh','mock/build_seed.py','mock/data/bonds_seed.json'):
        assert not (root/name).exists()
    from server import market_cache, qb_source
    assert market_cache.__name__ == 'server.market_cache'
    assert qb_source.__name__ == 'server.qb_source'
