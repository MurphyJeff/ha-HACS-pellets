"""Unit tests for the pure pellet model (no Home Assistant needed)."""

import importlib.util
import pathlib
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "pellets_model",
    pathlib.Path(__file__).parents[1] / "custom_components" / "pellets" / "model.py",
)
model = importlib.util.module_from_spec(_SPEC)
sys.modules["pellets_model"] = model
_SPEC.loader.exec_module(model)

H = 3600.0


def entry(ts_h, kind, **data):
    return {"id": f"{kind}-{ts_h}", "ts": ts_h * H, "type": kind, "data": data}


def seg(start_h, end_h, level):
    return [start_h * H, None if end_h is None else end_h * H, level]


def test_base_rates_from_feed_times():
    p = model.Params(feed_s={1: 3.2, 2: 4.8})
    rates = p.base_rates()
    assert rates[1] == pytest.approx(0.93)
    assert rates[2] == pytest.approx(0.93 * 4.8 / 3.2)
    assert rates[3] == model.MANUFACTURER_KGH[3]  # unknown feed -> datasheet


def test_burn_index_partial_overlap():
    idx = model.BurnIndex([seg(0, 2, 1), seg(3, None, 3)], {1: 1.0, 3: 2.0}, now=5 * H)
    assert idx.between(1 * H, 4 * H) == pytest.approx(1.0 + 2.0)
    assert idx.cumulative(10 * H) == pytest.approx(2.0 + 4.0)  # open segment capped at now


def test_full_fill_sets_reservoir_and_consumes():
    p = model.Params(initial_factor=1.0)
    st = model.replay([entry(0, "fill", bags=1, full=True)], [seg(0, 5, 1)], p, now=10 * H)
    assert st.reservoir_kg == pytest.approx(20 - 5 * 0.93)
    assert st.total_consumed_kg == pytest.approx(5 * 0.93)


def test_calibration_between_two_full_fills():
    p = model.Params(initial_factor=1.0)
    # 10 h at P3 (1.77 kg/h) -> model 17.7 kg ; real refill 1 bag at full -> 15 kg consumed.
    entries = [entry(0, "fill", bags=1, full=True), entry(12, "fill", bags=1, full=True)]
    st = model.replay(entries, [seg(1, 11, 3)], p, now=12 * H)
    assert st.calibration_count == 1
    assert st.factor == pytest.approx(1.0 + 0.5 * (15 / 17.7 - 1.0))  # first calibration: gain 0.5
    assert st.reservoir_kg == pytest.approx(20)


def test_partial_fill_between_anchors_counts_in_calibration():
    p = model.Params(initial_factor=1.0)
    entries = [
        entry(0, "fill", bags=1, full=True),
        entry(6, "fill", bags=0.5),  # 7.5 kg poured, not to full
        entry(20, "empty"),
    ]
    st = model.replay(entries, [seg(1, 19, 1)], p, now=20 * H)
    # real consumed = 20 + 7.5 - 0 = 27.5 ; model = 18 h * 0.93
    assert st.calibrations[0].observed == pytest.approx(27.5 / (18 * 0.93))
    assert st.reservoir_kg == 0


def test_outlier_rejected_after_two_calibrations():
    p = model.Params(initial_factor=1.0, max_gain=0.3)
    entries = [entry(0, "fill", bags=1, full=True)]
    segs = []
    for i in range(1, 4):  # three consistent cycles at factor 1.0
        segs.append(seg(i * 20 - 19, i * 20 - 19 + 15 / 1.77, 3))
        entries.append(entry(i * 20, "fill", bags=1, full=True))
    segs.append(seg(61, 61 + 15 / 1.77, 3))
    entries.append(entry(80, "fill", bags=0.2, full=True))  # absurd: 3 kg for 15 kg burnt
    st = model.replay(entries, segs, p, now=80 * H)
    assert st.calibrations[-1].accepted is False
    assert st.factor == pytest.approx(1.0, abs=1e-6)
    assert any(w["type"] == "calibration_outlier" for w in st.warnings)


def test_stocks_purchase_transfer_and_open_bag():
    p = model.Params()
    entries = [
        entry(0, "purchase", bags=100, total_eur=650, location="supplier"),
        entry(1, "transfer", bags=7, supplier_remaining=93),
        entry(2, "fill", bags=1.15, full=True),
    ]
    st = model.replay(entries, [], p, now=3 * H)
    assert st.supplier_bags == 93
    assert st.home_bags == pytest.approx(5.85)
    assert st.open_bag_fraction == pytest.approx(0.85)
    assert st.avg_price == pytest.approx(6.5)


def test_supplier_sheet_wins_and_warns():
    p = model.Params()
    entries = [
        entry(0, "purchase", bags=100, price_per_bag=6.5),
        entry(1, "transfer", bags=7, supplier_remaining=90),  # sheet says 90 (3 bags taken earlier)
    ]
    st = model.replay(entries, [], p, now=2 * H)
    assert st.supplier_bags == 90
    assert st.warnings[0]["type"] == "supplier_mismatch"


def test_editing_old_entry_recomputes_everything():
    p = model.Params(initial_factor=1.0)
    entries = [entry(0, "fill", bags=1, full=True), entry(12, "fill", bags=1, full=True)]
    segs = [seg(1, 11, 3)]
    before = model.replay(entries, segs, p, now=12 * H).factor
    entries[1]["data"]["bags"] = 1.2  # correction from the UI
    after = model.replay(entries, segs, p, now=12 * H).factor
    assert after == pytest.approx(1.0 + 0.5 * (18 / 17.7 - 1.0))
    assert after != before


def test_inventory_soft_anchor_and_uncertainty():
    p = model.Params(initial_factor=1.0)
    st = model.replay([entry(0, "inventory", home_bags=6, reservoir_kg=10)], [seg(0, 2, 1)], p, now=2 * H)
    assert st.reservoir_kg == pytest.approx(10 - 2 * 0.93)
    assert st.soft_anchor is True
    assert st.reservoir_sigma_kg == pytest.approx(0.15 * 2 * 0.93 + 2.0)


def test_unknown_reservoir_until_first_anchor():
    st = model.replay([entry(0, "fill", bags=1)], [], model.Params(), now=H)
    assert st.reservoir_kg is None


def test_burn_stats_sessions_levels_and_ignitions():
    p = model.Params(initial_factor=1.0)
    rates = p.base_rates()
    # Session 1: 2 h P3 then 1 h P1 (level change, same session); session 2 later: 1 h P2.
    segs = [seg(1, 3, 3), seg(3, 4, 1), seg(6, 7, 2)]
    st = model.burn_stats(segs, p, 0.8, 0, 10 * H)
    assert st["hours"] == pytest.approx(4)
    assert st["hours_by_level"] == {1: 1.0, 2: 1.0, 3: 2.0}
    assert st["ignitions"] == 2
    assert len(st["sessions"]) == 2
    assert st["kg"] == pytest.approx(0.8 * (2 * rates[3] + rates[1] + rates[2]))
    assert st["sessions"][0]["avg_level"] == pytest.approx((2 * 3 + 1) / 3, abs=0.05)
    # A window starting mid-session counts the burn but not the ignition.
    part = model.burn_stats(segs, p, 0.8, 2 * H, 5 * H)
    assert part["hours"] == pytest.approx(2) and part["ignitions"] == 0


def test_maintenance_has_no_stock_effect_and_tracks_cleaning():
    p = model.Params()
    entries = [
        entry(0, "purchase", bags=10, price_per_bag=6.5, location="home"),
        {**entry(1, "maintenance", cost_eur=160), "data": {"kind": "ramonage", "cost_eur": 160}},
        {**entry(2, "maintenance"), "data": {"kind": "nettoyage"}},
    ]
    st = model.replay(entries, [], p, now=3 * H)
    assert st.home_bags == 10 and st.avg_price == pytest.approx(6.5)
    assert st.maintenance_eur == 160
    assert model.last_cleaning_ts(st) == 2 * H


# --- programme du poêle -----------------------------------------------------
from datetime import datetime, timezone  # noqa: E402

UTC = timezone.utc
P6 = model.Slot(6 * 60 + 20, 22 * 60 + 30, frozenset(range(7)))  # 06:20–22:30 tous les jours


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


def test_schedule_windows_merge_and_clip():
    w = model.schedule_windows([P6], ts(2026, 11, 23, 12), ts(2026, 11, 24, 12), UTC)
    assert w == [(ts(2026, 11, 23, 12), ts(2026, 11, 23, 22, 30)), (ts(2026, 11, 24, 6, 20), ts(2026, 11, 24, 12))]


def test_window_past_midnight():
    night = model.Slot(22 * 60, 2 * 60, frozenset({0}))  # lundi 22:00 → mardi 02:00
    w = model.schedule_windows([night], ts(2026, 11, 23, 0), ts(2026, 11, 25, 0), UTC)  # 23/11/2026 = lundi
    assert w == [(ts(2026, 11, 23, 22), ts(2026, 11, 24, 2))]


def test_project_empty_skips_night():
    # 10 kg à 1 kg par heure de programme, il est 20:00 : 2,5 h ce soir, puis 7,5 h demain dès 06:20
    eta = model.project_empty(10, 1.0, [P6], ts(2026, 11, 23, 20), UTC)
    assert eta == pytest.approx(ts(2026, 11, 24, 13, 50))


def test_project_empty_edge_cases():
    now = ts(2026, 11, 23, 3)
    assert model.project_empty(0, 1.0, [P6], now, UTC) == now
    assert model.project_empty(5, 0.0, [P6], now, UTC) is None
    assert model.project_empty(5, 1.0, [], now, UTC) is None
    assert model.project_empty(5, 1.0, [P6], now, UTC) == pytest.approx(ts(2026, 11, 23, 11, 20))


def test_unmeasured_full_fill_anchors_without_calibrating():
    """Un plein « quantité estimée » (measured=False) recale le réservoir mais ne touche pas au facteur."""
    from custom_components.pellets import model

    p = model.Params(capacity_kg=20, bag_kg=15, initial_factor=1.0, feed_s={1: 3.2, 2: 4.8, 3: 6.5, 4: 8.2, 5: 9.9})
    h = 3600.0
    segs = [[0.0, 10 * h, 1]]
    base = [{"id": "a", "ts": 0.0, "type": "fill", "data": {"bags": 1.0, "full": True}}]
    echo = base + [{"id": "b", "ts": 10 * h, "type": "fill", "data": {"bags": 0.5, "full": True, "measured": False}}]
    st = model.replay(echo, segs, p, 10 * h + 1)
    assert st.factor == 1.0
    assert not st.calibrations
    assert st.reservoir_kg == 20.0
    measured = base + [{"id": "b", "ts": 10 * h, "type": "fill", "data": {"bags": 0.5, "full": True}}]
    st2 = model.replay(measured, segs, p, 10 * h + 1)
    assert st2.calibrations and st2.factor != 1.0
