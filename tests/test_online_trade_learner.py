from pathlib import Path

from trading.online_trade_learner import OnlineTradeLearner, trade_features_from_signal


def test_feature_extraction():
    features = trade_features_from_signal(
        {
            "PumpPct": 4.2,
            "MomentumScore": 71,
            "OrderFlowScore": 66,
            "OrderFlowSpreadPct": 0.4,
            "ChasePct": 0.2,
        }
    )
    assert features["pump_pct"] == 4.2
    assert features["momentum"] == 71.0
    assert features["order_flow_score"] == 66.0


def test_learner_is_neutral_until_enough_samples(tmp_path: Path):
    model = OnlineTradeLearner(
        path=str(tmp_path / "model.json"),
        min_samples=5,
        learning_rate=0.1,
    )
    for _ in range(4):
        model.accept({"pump_pct": 4.0, "order_flow_score": 70.0}, True)
    assert model.predict_probability({"pump_pct": 4.0, "order_flow_score": 70.0}) == 0.5


def test_learner_learns_and_persists(tmp_path: Path):
    path = tmp_path / "model.json"
    model = OnlineTradeLearner(path=str(path), min_samples=1, learning_rate=0.1)
    for _ in range(40):
        model.accept({"pump_pct": 5.0, "order_flow_score": 80.0}, True)
        model.accept({"pump_pct": 3.1, "order_flow_score": 25.0}, False)

    good = model.predict_probability({"pump_pct": 5.0, "order_flow_score": 80.0})
    bad = model.predict_probability({"pump_pct": 3.1, "order_flow_score": 25.0})
    assert good > bad
    assert path.exists()

    restored = OnlineTradeLearner(path=str(path), min_samples=1)
    assert restored.samples == model.samples
    assert restored.predict_probability({"pump_pct": 5.0, "order_flow_score": 80.0}) > 0.5
