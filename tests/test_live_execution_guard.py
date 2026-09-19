from pathlib import Path

from trading.idempotency import IdempotencyGuard
from core.database import Database


def test_idempotency_prepares_and_blocks_same_client_id(tmp_path):
    db = Database(str(Path(tmp_path) / "orders.db"))
    guard = IdempotencyGuard(db)
    cid = guard.prepare("BTCIRT", "buy", "market", 0.01, client_order_id="test-fixed-id")
    assert cid == "test-fixed-id"
    assert guard.is_duplicate(cid)

    try:
        guard.prepare("BTCIRT", "buy", "market", 0.01, client_order_id=cid)
    except RuntimeError as exc:
        assert "Duplicate order blocked" in str(exc)
    else:
        raise AssertionError("duplicate order was not blocked")


def test_failed_idempotent_order_can_be_reused(tmp_path):
    db = Database(str(Path(tmp_path) / "orders.db"))
    guard = IdempotencyGuard(db)
    cid = guard.prepare("ETHIRT", "buy", "market", 0.1, client_order_id="retry-id")
    guard.mark_failed(cid, "simulated rejection")
    assert not guard.is_duplicate(cid)
    assert guard.prepare("ETHIRT", "buy", "market", 0.1, client_order_id=cid) == cid
