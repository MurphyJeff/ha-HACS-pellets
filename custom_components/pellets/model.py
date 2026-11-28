"""Pure pellet model: journal replay, consumption estimate and self-calibration.

No Home Assistant import here so the logic can be unit-tested in isolation.

Concepts
--------
* **Journal** (event sourcing): list of entries ``{"id", "ts", "type", "data"}``.
  Stocks, reservoir level and calibration are *never* stored; they are
  recomputed by replaying the journal. Editing or deleting an old entry
  therefore fixes everything downstream.
* **Burn segments**: ``[start_ts, end_ts | None, power_level]`` periods during
  which the stove was burning pellets (status WORKING) at a given power level.
* **Consumption model**: ``kg = factor * sum(hours_at_level * base_rate[level])``.
  ``base_rate`` comes from the auger feed times (if known) or the manufacturer
  table; ``factor`` is learned from anchors.
* **Anchors**: moments where the real reservoir level is known — a fill "up to
  full" (level before = capacity - poured), an "empty" observation (level = 0)
  or an inventory with a reservoir estimate (soft anchor).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import math
from typing import Any

HOUR = 3600.0
AUGER_CYCLE_S = 12.0

# Fallback rates for a typical 12 kW pellet stove (~0.9 kg/h at P1, ~2.6 kg/h at P5).
MANUFACTURER_KGH: dict[int, float] = {1: 0.93, 2: 1.35, 3: 1.77, 4: 2.18, 5: 2.6}

ENTRY_FILL = "fill"
ENTRY_EMPTY = "empty"
ENTRY_PURCHASE = "purchase"
ENTRY_TRANSFER = "transfer"
ENTRY_INVENTORY = "inventory"
ENTRY_MAINTENANCE = "maintenance"
ENTRY_TYPES = (ENTRY_FILL, ENTRY_EMPTY, ENTRY_PURCHASE, ENTRY_TRANSFER, ENTRY_INVENTORY, ENTRY_MAINTENANCE)

# Maintenance kinds: no effect on stock or consumption; "cleaning" kinds reset the cleaning counter.
MAINTENANCE_KINDS = ("nettoyage", "entretien", "ramonage", "reparation", "autre")
CLEANING_KINDS = ("nettoyage", "entretien", "ramonage")

LOCATION_HOME = "home"
LOCATION_SUPPLIER = "supplier"

FACTOR_MIN = 0.3
FACTOR_MAX = 2.0
DEFAULT_REL_ERROR = 0.15
SOFT_ANCHOR_SIGMA_KG = 2.0


@dataclass
class Params:
    """Model parameters (from the integration options)."""

    capacity_kg: float = 20.0
    bag_kg: float = 15.0
    initial_factor: float = 1.0
    feed_s: dict[int, float | None] = field(default_factory=dict)
    max_gain: float = 0.3
    outlier_ratio: float = 0.4
    min_model_kg: float = 4.0
    soft_anchor_weight: float = 0.5

    def base_rates(self) -> dict[int, float]:
        """kg/h per power level before the learned factor.

        With auger feed times known, consumption is proportional to the feed
        time per 12 s cycle; P1 is pinned to the manufacturer value so the
        learned factor stays close to 1 when the stove matches its datasheet.
        """
        rates = dict(MANUFACTURER_KGH)
        ref = self.feed_s.get(1)
        if ref:
            for level, feed in self.feed_s.items():
                if feed and level in rates:
                    rates[level] = MANUFACTURER_KGH[1] * feed / ref
        return rates


@dataclass
class Calibration:
    ts: float
    observed: float
    factor_before: float
    factor_after: float
    error: float
    accepted: bool
    weight: float


@dataclass
class State:
    """Result of a journal replay at ``now``."""

    now: float
    factor: float
    reservoir_kg: float | None = None
    reservoir_sigma_kg: float | None = None
    home_bags: float = 0.0
    supplier_bags: float = 0.0
    total_consumed_kg: float = 0.0
    purchased_bags: float = 0.0
    purchased_eur: float = 0.0
    calibrations: list[Calibration] = field(default_factory=list)
    last_anchor_ts: float | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    consumed_since_anchor_kg: float = 0.0
    soft_anchor: bool = False
    maintenance_eur: float = 0.0
    last_maintenance: dict[str, float] = field(default_factory=dict)

    @property
    def calibration_count(self) -> int:
        return sum(1 for c in self.calibrations if c.accepted)

    @property
    def mean_error(self) -> float | None:
        errs = [c.error for c in self.calibrations if c.accepted][-5:]
        return sum(errs) / len(errs) if errs else None

    @property
    def open_bag_fraction(self) -> float:
        frac = round(self.home_bags - math.floor(self.home_bags + 1e-9), 2)
        return 0.0 if frac in (0.0, 1.0) else frac

    @property
    def avg_price(self) -> float | None:
        return self.purchased_eur / self.purchased_bags if self.purchased_bags else None


class BurnIndex:
    """Fast cumulative 'weighted burn hours' lookup over burn segments."""

    def __init__(self, segments: list[list[Any]], rates: dict[int, float], now: float) -> None:
        segs = sorted(
            ((float(s), float(e) if e is not None else now, int(lvl)) for s, e, lvl in segments),
            key=lambda x: x[0],
        )
        self._starts: list[float] = []
        self._ends: list[float] = []
        self._rates: list[float] = []
        self._cum: list[float] = [0.0]
        for start, end, level in segs:
            end = min(end, now)
            if end <= start:
                continue
            rate = rates.get(level, rates.get(3, 1.77))
            self._starts.append(start)
            self._ends.append(end)
            self._rates.append(rate)
            self._cum.append(self._cum[-1] + rate * (end - start) / HOUR)

    def cumulative(self, t: float) -> float:
        """Weighted burn (kg at factor 1) from the beginning up to ``t``."""
        i = bisect_right(self._starts, t)
        if i == 0:
            return 0.0
        total = self._cum[i - 1]
        start, end, rate = self._starts[i - 1], self._ends[i - 1], self._rates[i - 1]
        return total + rate * (min(t, end) - start) / HOUR

    def between(self, t0: float, t1: float) -> float:
        return 0.0 if t1 <= t0 else self.cumulative(t1) - self.cumulative(t0)


def sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda e: (float(e["ts"]), e.get("created", 0)))


def replay(
    entries: list[dict[str, Any]],
    segments: list[list[Any]],
    params: Params,
    now: float,
) -> State:
    """Replay the journal and return the state at ``now``."""
    burn = BurnIndex(segments, params.base_rates(), now)
    state = State(now=now, factor=params.initial_factor)
    cap = params.capacity_kg
    bag = params.bag_kg

    level: float | None = None
    anchor_ts: float | None = None
    anchor_level = 0.0
    anchor_weight = 1.0
    poured_since_anchor = 0.0
    prev_ts: float | None = None

    def calibrate(ts: float, actual_kg: float) -> None:
        model_unit = burn.between(anchor_ts, ts)  # type: ignore[arg-type]
        if model_unit * state.factor < params.min_model_kg or actual_kg <= 0:
            return
        observed = actual_kg / model_unit
        before = state.factor
        error = abs(actual_kg - before * model_unit) / actual_kg
        accepted = state.calibration_count < 2 or abs(observed / before - 1) <= params.outlier_ratio
        after = before
        if accepted:
            # Fast early learning (0.5, 0.33, ...) then a floor so drift keeps being tracked.
            gain = max(1.0 / (state.calibration_count + 2), params.max_gain) * anchor_weight
            after = min(max(before + gain * (observed - before), FACTOR_MIN), FACTOR_MAX)
            state.factor = after
        else:
            state.warnings.append(
                {"ts": ts, "type": "calibration_outlier", "observed": round(observed, 3), "factor": round(before, 3)}
            )
        state.calibrations.append(Calibration(ts, observed, before, after, error, accepted, anchor_weight))

    for entry in sort_entries(entries):
        ts = float(entry["ts"])
        kind = entry["type"]
        data = entry.get("data", {})

        if prev_ts is not None:
            consumed = state.factor * burn.between(prev_ts, ts)
            state.total_consumed_kg += consumed
            if level is not None:
                level = max(level - consumed, 0.0)
        prev_ts = ts

        if kind == ENTRY_FILL:
            bags = float(data.get("bags", 0.0))
            kg = bags * bag
            state.home_bags -= bags
            if data.get("full"):
                # measured=False : quantité inconnue (bouton « plein » sans quantité, estimation du modèle).
                # Le réservoir est recalé à plein, mais la calibration est sautée : sinon le modèle se confirme lui-même.
                if anchor_ts is not None and data.get("measured", True):
                    calibrate(ts, anchor_level + poured_since_anchor - max(cap - kg, 0.0))
                level, anchor_ts, anchor_level, anchor_weight, poured_since_anchor = cap, ts, cap, 1.0, 0.0
            else:
                poured_since_anchor += kg
                if level is not None:
                    if level + kg > cap + 0.5:
                        state.warnings.append({"ts": ts, "type": "overfill", "estimated_kg": round(level + kg, 1)})
                    level = min(level + kg, cap)
        elif kind == ENTRY_EMPTY:
            if anchor_ts is not None:
                calibrate(ts, anchor_level + poured_since_anchor)
            level, anchor_ts, anchor_level, anchor_weight, poured_since_anchor = 0.0, ts, 0.0, 1.0, 0.0
        elif kind == ENTRY_PURCHASE:
            bags = float(data.get("bags", 0.0))
            if data.get("location", LOCATION_SUPPLIER) == LOCATION_HOME:
                state.home_bags += bags
            else:
                state.supplier_bags += bags
            state.purchased_bags += bags
            price = data.get("total_eur")
            if price is None:
                price = bags * float(data.get("price_per_bag", 0.0))
            state.purchased_eur += float(price)
        elif kind == ENTRY_TRANSFER:
            bags = float(data.get("bags", 0.0))
            state.supplier_bags -= bags
            state.home_bags += bags
            remaining = data.get("supplier_remaining")
            if remaining is not None:
                remaining = float(remaining)
                if abs(remaining - state.supplier_bags) > 0.01:
                    state.warnings.append(
                        {"ts": ts, "type": "supplier_mismatch", "expected": state.supplier_bags, "sheet": remaining}
                    )
                state.supplier_bags = remaining
        elif kind == ENTRY_MAINTENANCE:
            state.maintenance_eur += float(data.get("cost_eur") or 0.0)
            state.last_maintenance[str(data.get("kind", "autre"))] = ts
        elif kind == ENTRY_INVENTORY:
            if data.get("home_bags") is not None:
                state.home_bags = float(data["home_bags"])
            if data.get("supplier_bags") is not None:
                state.supplier_bags = float(data["supplier_bags"])
            if data.get("reservoir_kg") is not None:
                level = min(max(float(data["reservoir_kg"]), 0.0), cap)
                anchor_ts, anchor_level, anchor_weight = ts, level, params.soft_anchor_weight
                poured_since_anchor = 0.0

        if kind in (ENTRY_FILL, ENTRY_EMPTY, ENTRY_INVENTORY) and anchor_ts == ts:
            state.last_anchor_ts = ts
            state.soft_anchor = anchor_weight < 1.0

    # Consumption from the last entry until now.
    if prev_ts is not None:
        consumed = state.factor * burn.between(prev_ts, now)
        state.total_consumed_kg += consumed
        if level is not None:
            level = max(level - consumed, 0.0)

    state.reservoir_kg = level
    if level is not None and anchor_ts is not None:
        since = state.factor * burn.between(anchor_ts, now)
        state.consumed_since_anchor_kg = since
        rel = state.mean_error if state.mean_error is not None else DEFAULT_REL_ERROR
        sigma = rel * since + (SOFT_ANCHOR_SIGMA_KG if state.soft_anchor else 0.0)
        state.reservoir_sigma_kg = min(sigma, cap)
    return state


def consumed_between(segments: list[list[Any]], params: Params, factor: float, t0: float, t1: float) -> float:
    """Model consumption (kg) in a time window with a given factor."""
    return factor * BurnIndex(segments, params.base_rates(), t1).between(t0, t1)


def current_rate(params: Params, factor: float, level: int | None) -> float:
    """kg/h at the given power level (0 when not burning)."""
    if not level:
        return 0.0
    rates = params.base_rates()
    return factor * rates.get(int(level), rates[3])


SESSION_GAP_S = 15 * 60  # burn segments closer than this belong to the same session (level changes)


def burn_stats(segments: list[list[Any]], params: Params, factor: float, t0: float, t1: float) -> dict[str, Any]:
    """Burn hours (total and per level), kg, sessions and ignitions within [t0, t1]."""
    rates = params.base_rates()
    hours: dict[int, float] = {}
    kg = 0.0
    sessions: list[dict[str, Any]] = []
    for start, end, level in sorted(segments, key=lambda x: x[0]):
        end = t1 if end is None else min(float(end), t1)
        start = float(start)
        if end <= t0 or start >= t1:
            continue
        a, b = max(start, t0), end
        h = (b - a) / HOUR
        level = int(level)
        hours[level] = hours.get(level, 0.0) + h
        seg_kg = factor * rates.get(level, rates[3]) * h
        kg += seg_kg
        if sessions and start - sessions[-1]["end"] <= SESSION_GAP_S:
            cur = sessions[-1]
            cur["end"], cur["kg"], cur["hours"] = b, cur["kg"] + seg_kg, cur["hours"] + h
            cur["level_h"] += level * h
        else:
            sessions.append({"start": a, "end": b, "kg": seg_kg, "hours": h, "level_h": level * h,
                             "ignition": start >= t0})
    for sess in sessions:
        sess["avg_level"] = round(sess.pop("level_h") / sess["hours"], 1) if sess["hours"] else None
    return {
        "hours": sum(hours.values()),
        "hours_by_level": {lvl: round(h, 2) for lvl, h in sorted(hours.items())},
        "kg": kg,
        "sessions": sessions,
        "ignitions": sum(1 for x in sessions if x["ignition"]),
    }


def last_cleaning_ts(state: State) -> float | None:
    """Most recent cleaning-type maintenance, if any."""
    stamps = [ts for kind, ts in state.last_maintenance.items() if kind in CLEANING_KINDS]
    return max(stamps) if stamps else None


# ---------------------------------------------------------------------------
# Heating schedule (stove "chrono" programs) and schedule-aware empty forecast
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    """One weekly program: local minutes ``start``..``stop`` on ``days`` (0 = Monday)."""

    start: int
    stop: int
    days: frozenset[int]

    def label(self) -> str:
        names = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
        d = sorted(self.days)
        days = "tous les jours" if len(d) == 7 else ", ".join(names[i] for i in d)
        return f"{self.start // 60:02d}:{self.start % 60:02d}–{self.stop // 60:02d}:{self.stop % 60:02d} {days}"


def schedule_windows(slots: list[Slot], t0: float, t1: float, tz: Any) -> list[tuple[float, float]]:
    """Heating windows (timestamps, merged) overlapping [t0, t1]. A program whose stop is
    before its start runs past midnight."""
    from datetime import datetime, timedelta

    out: list[tuple[float, float]] = []
    day = datetime.fromtimestamp(t0, tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    end_day = datetime.fromtimestamp(t1, tz)
    while day <= end_day:
        for s in slots:
            if day.weekday() not in s.days:
                continue
            a = day + timedelta(minutes=s.start)
            b = day + timedelta(minutes=s.stop) + (timedelta(days=1) if s.stop <= s.start else timedelta())
            a_ts, b_ts = max(a.timestamp(), t0), min(b.timestamp(), t1)
            if b_ts > a_ts:
                out.append((a_ts, b_ts))
        day += timedelta(days=1)
    out.sort()
    merged: list[tuple[float, float]] = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def window_hours(slots: list[Slot], t0: float, t1: float, tz: Any) -> float:
    return sum(b - a for a, b in schedule_windows(slots, t0, t1, tz)) / HOUR


def project_empty(kg: float, kg_per_window_h: float, slots: list[Slot], now: float, tz: Any,
                  horizon_days: float = 21) -> float | None:
    """Timestamp at which ``kg`` runs out, burning ``kg_per_window_h`` only inside the
    heating windows. None if the reservoir outlasts the horizon or nothing is burned."""
    if kg <= 0:
        return now
    if kg_per_window_h <= 0 or not slots:
        return None
    left = kg
    for a, b in schedule_windows(slots, now, now + horizon_days * 86400, tz):
        burn = kg_per_window_h * (b - a) / HOUR
        if burn >= left:
            return a + left / kg_per_window_h * HOUR
        left -= burn
    return None
