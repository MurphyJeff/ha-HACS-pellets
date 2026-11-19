"""Runtime manager: persistence, stove tracking, journal operations, events."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import time
from typing import Any
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import model
from .const import (
    BURNING_STATUSES,
    CONF_BAG_KG,
    CONF_CAPACITY,
    CONF_CLIMATE_ENTITY,
    CONF_DEPLETED_ENTITY,
    CONF_FEED,
    CONF_HOME_LOW_BAGS,
    CONF_INITIAL_FACTOR,
    CONF_LOW_KG,
    CONF_POWER_ENTITY,
    CONF_STATUS_ENTITY,
    DEFAULTS,
    DEPLETED_CONFIRM_S,
    DOMAIN,
    EVENT,
    POSSIBLE_EMPTY_MARGIN_KG,
    SIGNAL_UPDATE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)
DAY = 86400.0


class PelletsManager:
    """Owns the journal, the burn segments and the computed state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.entries: list[dict[str, Any]] = []
        self.segments: list[list[Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.last_seen: float | None = None
        self.state: model.State | None = None
        self.derived: dict[str, Any] = {}
        self._unsubs: list[Any] = []
        self._depleted_since: float | None = None
        self._flags: dict[str, bool] = {}
        self._known_warnings: set[str] = set()

    # ------------------------------------------------------------------ config
    def conf(self, key: str) -> Any:
        return {**DEFAULTS, **self.entry.data, **self.entry.options}.get(key)

    @property
    def params(self) -> model.Params:
        feed = {lvl: self.conf(CONF_FEED.format(lvl)) for lvl in range(1, 6)}
        return model.Params(
            capacity_kg=float(self.conf(CONF_CAPACITY)),
            bag_kg=float(self.conf(CONF_BAG_KG)),
            initial_factor=float(self.conf(CONF_INITIAL_FACTOR)),
            feed_s={k: float(v) for k, v in feed.items() if v},
        )

    # --------------------------------------------------------------- lifecycle
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.entries = data.get("entries", [])
        self.segments = data.get("segments", [])
        self.audit = data.get("audit", [])
        self.last_seen = data.get("last_seen")
        self._flags = data.get("flags", {})
        self._known_warnings = set(data.get("known_warnings", []))
        # A segment left open by a restart/crash ends at the last heartbeat.
        if self.segments and self.segments[-1][1] is None:
            self.segments[-1][1] = self.last_seen or time.time()
        self._recompute(fire=False)

    async def async_start(self) -> None:
        ids = [
            self.conf(CONF_STATUS_ENTITY),
            self.conf(CONF_POWER_ENTITY),
            self.conf(CONF_CLIMATE_ENTITY),
            self.conf(CONF_DEPLETED_ENTITY),
        ]
        self._unsubs.append(async_track_state_change_event(self.hass, [i for i in ids if i], self._on_stove_change))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, timedelta(minutes=1)))
        self._update_burn(time.time())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.last_seen = time.time()
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "segments": self.segments,
            "audit": self.audit[-500:],
            "last_seen": self.last_seen,
            "flags": self._flags,
            "known_warnings": sorted(self._known_warnings),
        }

    def _save(self, delay: float = 1.0) -> None:
        self._store.async_delay_save(self._data, delay)

    # ------------------------------------------------------------ stove input
    def _burning_level(self) -> int | None:
        """Current power level if the stove burns pellets, else None."""
        status = self.hass.states.get(self.conf(CONF_STATUS_ENTITY) or "")
        burning: bool
        if status is not None and status.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            burning = status.state in BURNING_STATUSES
        else:
            climate = self.hass.states.get(self.conf(CONF_CLIMATE_ENTITY) or "")
            burning = climate is not None and climate.attributes.get("hvac_action") == "heating"
        if not burning:
            return None
        power = self.hass.states.get(self.conf(CONF_POWER_ENTITY) or "")
        try:
            return max(1, min(5, int(float(power.state))))  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            return 3

    def _update_burn(self, now: float) -> tuple[bool, bool]:
        """Open/close burn segments. Returns (was_burning, is_burning)."""
        level = self._burning_level()
        open_seg = self.segments[-1] if self.segments and self.segments[-1][1] is None else None
        was = open_seg is not None
        if open_seg is not None and (level is None or level != open_seg[2]):
            open_seg[1] = now
            open_seg = None
        if level is not None and open_seg is None:
            self.segments.append([now, None, level])
        if was != (level is not None) or (level is not None and was):
            self._save()
        return was, level is not None

    @callback
    def _on_stove_change(self, event: Event[EventStateChangedData]) -> None:
        now = time.time()
        entity_id = event.data["entity_id"]
        new = event.data["new_state"]
        if entity_id == self.conf(CONF_DEPLETED_ENTITY):
            if new is not None and new.state == "on" and self._burning_level() is not None:
                self._depleted_since = now
            return
        was, is_burning = self._update_burn(now)
        if was and not is_burning:
            self._on_burn_stopped(now)
        self._recompute()

    def _on_burn_stopped(self, now: float) -> None:
        st = self.state
        estimate = st.reservoir_kg if st is not None else None
        if self._depleted_since is not None and now - self._depleted_since <= DEPLETED_CONFIRM_S:
            # Not recorded automatically: a stove "depleted" flag can be raised
            # right after a refill. The user confirms.
            self._depleted_since = None
            msg = "Poêle arrêté, alarme pellets épuisés."
            if estimate is not None:
                msg += f" Estimation : ~{estimate:.1f} kg."
            self._fire("empty_detected", message=msg + " Réservoir vide ?", reservoir_kg=estimate)
            return
        if st is not None and st.reservoir_kg is not None:
            sigma = st.reservoir_sigma_kg or 0.0
            if st.reservoir_kg <= POSSIBLE_EMPTY_MARGIN_KG + sigma:
                self._fire(
                    "possible_empty",
                    message=f"Poêle arrêté avec ~{st.reservoir_kg:.1f} kg estimés : réservoir vide ?",
                    reservoir_kg=round(st.reservoir_kg, 1),
                )

    @callback
    def _on_tick(self, _now: datetime) -> None:
        now = time.time()
        self.last_seen = now
        if self._depleted_since is not None and now - self._depleted_since > DEPLETED_CONFIRM_S:
            self._depleted_since = None
        self._update_burn(now)
        self._save(delay=60)
        self._recompute()

    # ----------------------------------------------------------------- journal
    def add_entry(
        self, kind: str, data: dict[str, Any], ts: float | None = None, source: str = "user"
    ) -> dict[str, Any]:
        if kind not in model.ENTRY_TYPES:
            raise ValueError(f"unknown entry type {kind}")
        now = time.time()
        entry = {
            "id": uuid.uuid4().hex[:8],
            "ts": float(ts if ts is not None else now),
            "created": now,
            "type": kind,
            "data": {k: v for k, v in data.items() if v is not None},
            "source": source,
        }
        self.entries.append(entry)
        self._audit("add", entry["id"], None, entry)
        if kind == model.ENTRY_FILL:
            self._flags["reservoir_low"] = False
        self._save()
        self._recompute()
        if source != "setup":
            self._fire("entry_added", entry_id=entry["id"], summary=self.describe(entry))
        return entry

    async def async_bootstrap(self, start: dict[str, Any]) -> None:
        """Seed an empty journal from the setup wizard's starting point (runs once)."""
        if self.entries:
            return
        now = time.time()
        price = start.get("price_per_bag")
        for location, key in ((model.LOCATION_HOME, "home_bags"), (model.LOCATION_SUPPLIER, "supplier_bags")):
            bags = float(start.get(key) or 0)
            if bags and price:
                self.add_entry(
                    model.ENTRY_PURCHASE,
                    {"bags": bags, "price_per_bag": float(price), "location": location},
                    ts=now - 2, source="setup",
                )
        level = start.get("reservoir_level")
        self.add_entry(
            model.ENTRY_INVENTORY,
            {
                "home_bags": float(start.get("home_bags") or 0),
                "supplier_bags": float(start.get("supplier_bags") or 0),
                "reservoir_kg": float(level) * self.params.capacity_kg if level not in (None, "") else None,
            },
            ts=now - 1, source="setup",
        )
        days = int(start.get("history_days") or 0)
        if days:
            try:
                await self.async_import_history(days)
            except Exception:  # noqa: BLE001 - recorder may be missing; the journal is still valid
                _LOGGER.warning("History import skipped", exc_info=True)

    def edit_entry(self, entry_id: str, ts: float | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
        entry = self._find(entry_id)
        before = {**entry, "data": dict(entry["data"])}
        if ts is not None:
            entry["ts"] = float(ts)
        if data:
            for key, value in data.items():
                if value is None:
                    entry["data"].pop(key, None)
                else:
                    entry["data"][key] = value
        entry["edited"] = time.time()
        self._audit("edit", entry_id, before, entry)
        self._save()
        self._recompute()
        return entry

    def delete_entry(self, entry_id: str) -> dict[str, Any]:
        entry = self._find(entry_id)
        self.entries.remove(entry)
        self._audit("delete", entry_id, entry, None)
        self._save()
        self._recompute()
        return entry

    def undo_last(self) -> dict[str, Any] | None:
        if not self.entries:
            return None
        # Entries are appended in creation order; edits never reorder them.
        return self.delete_entry(self.entries[-1]["id"])

    def _find(self, entry_id: str) -> dict[str, Any]:
        for entry in self.entries:
            if entry["id"] == entry_id:
                return entry
        raise KeyError(entry_id)

    def _audit(self, action: str, entry_id: str, before: Any, after: Any) -> None:
        self.audit.append({"at": time.time(), "action": action, "id": entry_id, "before": before, "after": after})

    def describe(self, entry: dict[str, Any]) -> str:
        when = dt_util.as_local(dt_util.utc_from_timestamp(entry["ts"])).strftime("%d/%m %H:%M")
        d = entry["data"]
        kind = entry["type"]
        if kind == model.ENTRY_FILL:
            txt = f"remplissage {fmt_bags(d.get('bags', 0))} sac" + (" (plein)" if d.get("full") else "")
        elif kind == model.ENTRY_EMPTY:
            txt = "réservoir vide" + (" (auto)" if entry.get("source") == "auto" else "")
        elif kind == model.ENTRY_PURCHASE:
            where = "maison" if d.get("location") == model.LOCATION_HOME else "fournisseur"
            txt = f"achat {fmt_bags(d.get('bags', 0))} sacs → {where}"
        elif kind == model.ENTRY_TRANSFER:
            txt = f"retrait fournisseur {fmt_bags(d.get('bags', 0))} sacs"
            if d.get("supplier_remaining") is not None:
                txt += f" (fiche : {fmt_bags(d['supplier_remaining'])})"
        else:
            parts = [
                f"{label} {fmt_bags(d[key])}"
                for key, label in (("home_bags", "maison"), ("supplier_bags", "fournisseur"), ("reservoir_kg", "réservoir kg"))
                if d.get(key) is not None
            ]
            txt = "inventaire " + ", ".join(parts)
        return f"{when} — {txt}"

    async def async_import_history(self, days: int) -> int:
        """Rebuild burn segments of the last ``days`` days from the recorder."""
        from homeassistant.components.recorder import get_instance, history

        end = dt_util.utcnow()
        start = end - timedelta(days=days)
        status_id = self.conf(CONF_STATUS_ENTITY)
        climate_id = self.conf(CONF_CLIMATE_ENTITY)
        power_id = self.conf(CONF_POWER_ENTITY)
        ids = [i for i in (status_id, climate_id, power_id) if i]
        states = await get_instance(self.hass).async_add_executor_job(
            lambda: history.get_significant_states(self.hass, start, end, ids, None, True, False, False, False)
        )
        # The stove status is the reliable burn signal: the thermostat reports "heating"
        # through standby and ignition too. Fall back to it only without status history.
        use_status = bool(states.get(status_id))
        timeline: list[tuple[float, str, Any]] = []
        if use_status:
            for st in states.get(status_id, []):
                timeline.append((st.last_changed.timestamp(), "b", st.state in BURNING_STATUSES))
        else:
            for st in states.get(climate_id, []):
                timeline.append((st.last_changed.timestamp(), "b", st.attributes.get("hvac_action") == "heating"))
        for st in states.get(power_id, []):
            try:
                timeline.append((st.last_changed.timestamp(), "p", int(float(st.state))))
            except (TypeError, ValueError):
                continue
        timeline.sort(key=lambda x: x[0])
        new: list[list[Any]] = []
        heating, level = False, 3
        for ts, kind, value in timeline:
            if kind == "b":
                heating = value
            else:
                level = max(1, min(5, value))
            if new and new[-1][1] is None and (not heating or new[-1][2] != level):
                new[-1][1] = ts
            if heating and (not new or new[-1][1] is not None):
                new.append([ts, None, level])
        if new and new[-1][1] is None:
            new[-1][1] = end.timestamp()
        t0 = start.timestamp()
        kept = [s for s in self.segments if (s[1] or time.time()) <= t0]
        open_now = self.segments[-1] if self.segments and self.segments[-1][1] is None else None
        self.segments = kept + [s for s in new if s[0] >= t0]
        if open_now is not None and self.segments and self.segments[-1][1] is not None:
            self.segments[-1][1] = None  # keep tracking the current burn
        self._audit("import_history", "-", None, {"days": days, "segments": len(new)})
        self._save()
        self._recompute()
        return len(new)

    # ---------------------------------------------------------------- compute
    def _recompute(self, fire: bool = True) -> None:
        now = time.time()
        params = self.params
        st = model.replay(self.entries, self.segments, params, now)
        self.state = st
        first = min((s[0] for s in self.segments), default=now)

        def avg_per(window_s: float) -> float | None:
            span = min(window_s, now - first)
            if span < 6 * 3600:
                return None
            return model.consumed_between(self.segments, params, st.factor, now - span, now) / span

        per_s_week = avg_per(7 * DAY)
        per_s_3d = avg_per(3 * DAY)
        open_seg = self.segments[-1] if self.segments and self.segments[-1][1] is None else None
        daily = per_s_week * DAY if per_s_week else None
        hourly = per_s_3d * 3600 if per_s_3d else None
        reservoir = st.reservoir_kg
        hours_left = reservoir / hourly if reservoir is not None and hourly else None
        self.derived = {
            "burn_rate": model.current_rate(params, st.factor, open_seg[2] if open_seg else None),
            "power_level": open_seg[2] if open_seg else 0,
            "daily_kg": daily,
            "hours_left": hours_left,
            "empty_eta": dt_util.utc_from_timestamp(now + hours_left * 3600) if hours_left is not None else None,
            "autonomy_home_days": (st.home_bags * params.bag_kg + (reservoir or 0)) / daily if daily else None,
            "autonomy_total_days": (
                ((st.home_bags + st.supplier_bags) * params.bag_kg + (reservoir or 0)) / daily if daily else None
            ),
            "free_kg": params.capacity_kg - reservoir if reservoir is not None else None,
            "suggested_fill_bags": (params.capacity_kg - reservoir) / params.bag_kg if reservoir is not None else None,
        }
        if fire:
            self._check_alerts(st, params)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    def _check_alerts(self, st: model.State, params: model.Params) -> None:
        low = st.reservoir_kg is not None and st.reservoir_kg <= float(self.conf(CONF_LOW_KG))
        if low and not self._flags.get("reservoir_low"):
            self._flags["reservoir_low"] = True
            self._fire(
                "reservoir_low",
                message=f"Réservoir ~{st.reservoir_kg:.1f} kg (±{(st.reservoir_sigma_kg or 0):.1f}).",
                reservoir_kg=round(st.reservoir_kg or 0, 1),
            )
        home_low = st.home_bags <= float(self.conf(CONF_HOME_LOW_BAGS))
        if home_low and not self._flags.get("home_low"):
            self._fire("home_stock_low", message=f"Stock maison : {fmt_bags(st.home_bags)} sacs.", home_bags=st.home_bags)
        self._flags["home_low"] = home_low
        for warning in st.warnings:
            key = f"{warning['type']}:{warning['ts']}"
            if key not in self._known_warnings:
                self._known_warnings.add(key)
                self._fire("warning", warning_type=warning["type"], **{k: v for k, v in warning.items() if k != "type"})

    def _fire(self, kind: str, **data: Any) -> None:
        self.hass.bus.async_fire(EVENT, {"type": kind, "entry_id": self.entry.entry_id, **data})

    def journal(self, limit: int) -> list[dict[str, Any]]:
        recent = sorted(self.entries, key=lambda e: e["ts"], reverse=True)[:limit]
        return [{"id": e["id"], "text": self.describe(e), "type": e["type"], **e["data"]} for e in recent]


def fmt_bags(value: Any) -> str:
    try:
        return f"{float(value):g}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value)
