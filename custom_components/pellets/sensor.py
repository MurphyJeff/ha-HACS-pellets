"""Pellets sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfMass, UnitOfTime
from homeassistant.util import dt as dt_util
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, JOURNAL_ATTR_SIZE, SIGNAL_UPDATE
from .manager import PelletsManager

BAGS = "sacs"


@dataclass(frozen=True, kw_only=True)
class PelletsSensorDescription(SensorEntityDescription):
    value: Callable[[PelletsManager], Any]
    attrs: Callable[[PelletsManager], dict[str, Any]] | None = None


def _r(value: float | None, ndigits: int = 1) -> float | None:
    return None if value is None else round(value, ndigits)


def _st(m: PelletsManager) -> Any:
    return m.state


def _d(m: PelletsManager, period: str, key: str) -> Any:
    return (m.derived.get(period) or {}).get(key)


def _session_attrs(sess: dict[str, Any] | None) -> dict[str, Any]:
    if not sess:
        return {}
    return {
        "debut": dt_util.utc_from_timestamp(sess["start"]).isoformat(),
        "fin": dt_util.utc_from_timestamp(sess["end"]).isoformat(),
        "kg": round(sess["kg"], 2),
        "palier_moyen": sess["avg_level"],
    }


def _iso(ts: float | None) -> str | None:
    return None if ts is None else dt_util.utc_from_timestamp(ts).isoformat()


SENSORS: tuple[PelletsSensorDescription, ...] = (
    PelletsSensorDescription(
        key="maintenance_cost",
        translation_key="maintenance_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        suggested_display_precision=2,
        value=lambda m: _r(_st(m).maintenance_eur, 2),
        attrs=lambda m: {f"dernier_{k}": _iso(ts) for k, ts in _st(m).last_maintenance.items()},
    ),
    PelletsSensorDescription(
        key="kg_since_cleaning",
        translation_key="kg_since_cleaning",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=0,
        value=lambda m: _r(m.derived.get("kg_since_cleaning")),
    ),
    PelletsSensorDescription(
        key="consumption_today",
        translation_key="consumption_today",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        value=lambda m: _r(_d(m, "today", "kg")),
        attrs=lambda m: {"hier_kg": _r(_d(m, "yesterday", "kg")), "sept_jours_kg": _r(_d(m, "week", "kg"))},
    ),
    PelletsSensorDescription(
        key="energy",
        translation_key="energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        value=lambda m: _r(m.derived.get("energy_kwh"), 2),
    ),
    PelletsSensorDescription(
        key="burn_time_today",
        translation_key="burn_time_today",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value=lambda m: _r(_d(m, "today", "hours"), 2),
        attrs=lambda m: {
            "par_palier_h": _d(m, "today", "hours_by_level"),
            "hier_h": _r(_d(m, "yesterday", "hours"), 2),
            "hier_par_palier_h": _d(m, "yesterday", "hours_by_level"),
            "sept_jours_h": _r(_d(m, "week", "hours"), 1),
            "sept_jours_par_palier_h": _d(m, "week", "hours_by_level"),
        },
    ),
    PelletsSensorDescription(
        key="ignitions_today",
        translation_key="ignitions_today",
        value=lambda m: _d(m, "today", "ignitions"),
        attrs=lambda m: {"hier": _d(m, "yesterday", "ignitions"), "sept_jours": _d(m, "week", "ignitions")},
    ),
    PelletsSensorDescription(
        key="last_burn",
        translation_key="last_burn",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value=lambda m: _r((m.derived.get("last_session") or {}).get("hours"), 2),
        attrs=lambda m: _session_attrs(m.derived.get("last_session")),
    ),
    PelletsSensorDescription(
        key="cost_today",
        translation_key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="€",
        suggested_display_precision=2,
        value=lambda m: _r(m.derived.get("cost_today"), 2),
        attrs=lambda m: {"hier_eur": _r(m.derived.get("cost_yesterday"), 2)},
    ),
    PelletsSensorDescription(
        key="reservoir",
        translation_key="reservoir",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        value=lambda m: _r(_st(m).reservoir_kg),
        attrs=lambda m: {
            "incertitude_kg": _r(_st(m).reservoir_sigma_kg),
            "consomme_depuis_ancrage_kg": _r(_st(m).consumed_since_anchor_kg),
            "ancrage_approximatif": _st(m).soft_anchor,
            "place_libre_kg": _r(m.derived.get("free_kg")),
            "sacs_pour_le_plein": _r(m.derived.get("suggested_fill_bags"), 2),
        },
    ),
    PelletsSensorDescription(
        key="reservoir_percent",
        translation_key="reservoir_percent",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value=lambda m: None
        if _st(m).reservoir_kg is None
        else round(100 * _st(m).reservoir_kg / m.params.capacity_kg),
    ),
    PelletsSensorDescription(
        key="hours_left",
        translation_key="hours_left",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value=lambda m: _r(m.derived.get("hours_left")),
    ),
    PelletsSensorDescription(
        key="empty_eta",
        translation_key="empty_eta",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda m: m.derived.get("empty_eta"),
    ),
    PelletsSensorDescription(
        key="home_stock",
        translation_key="home_stock",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=BAGS,
        suggested_display_precision=2,
        value=lambda m: _r(_st(m).home_bags, 2),
        attrs=lambda m: {
            "sacs_pleins": int(_st(m).home_bags + 1e-9) if _st(m).home_bags > 0 else 0,
            "sac_entame": _st(m).open_bag_fraction,
        },
    ),
    PelletsSensorDescription(
        key="supplier_stock",
        translation_key="supplier_stock",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=BAGS,
        suggested_display_precision=0,
        value=lambda m: _r(_st(m).supplier_bags, 2),
    ),
    PelletsSensorDescription(
        key="total_stock",
        translation_key="total_stock",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=BAGS,
        suggested_display_precision=1,
        value=lambda m: _r(_st(m).home_bags + _st(m).supplier_bags, 2),
    ),
    PelletsSensorDescription(
        key="consumption",
        translation_key="consumption",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        value=lambda m: _r(_st(m).total_consumed_kg, 2),
    ),
    PelletsSensorDescription(
        key="daily_consumption",
        translation_key="daily_consumption",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kg/j",
        suggested_display_precision=1,
        value=lambda m: _r(m.derived.get("daily_kg")),
    ),
    PelletsSensorDescription(
        key="burn_rate",
        translation_key="burn_rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kg/h",
        suggested_display_precision=2,
        value=lambda m: _r(m.derived.get("burn_rate"), 2),
        attrs=lambda m: {"palier": m.derived.get("power_level")},
    ),
    PelletsSensorDescription(
        key="autonomy_home",
        translation_key="autonomy_home",
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=1,
        value=lambda m: _r(m.derived.get("autonomy_home_days")),
    ),
    PelletsSensorDescription(
        key="autonomy_total",
        translation_key="autonomy_total",
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        value=lambda m: _r(m.derived.get("autonomy_total_days")),
    ),
    PelletsSensorDescription(
        key="season_cost",
        translation_key="season_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        value=lambda m: _r(_st(m).purchased_eur, 2),
        attrs=lambda m: {"sacs_achetes": _st(m).purchased_bags},
    ),
    PelletsSensorDescription(
        key="avg_price",
        translation_key="avg_price",
        native_unit_of_measurement="€/sac",
        suggested_display_precision=2,
        value=lambda m: _r(_st(m).avg_price, 2),
    ),
    PelletsSensorDescription(
        key="calibration_factor",
        translation_key="calibration_factor",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value=lambda m: _r(_st(m).factor, 3),
        attrs=lambda m: {
            "calibrations": _st(m).calibration_count,
            "debits_kg_h": {lvl: round(r * _st(m).factor, 2) for lvl, r in m.params.base_rates().items()},
            "historique": [
                {"ts": c.ts, "observe": round(c.observed, 3), "apres": round(c.factor_after, 3), "accepte": c.accepted}
                for c in _st(m).calibrations[-10:]
            ],
        },
    ),
    PelletsSensorDescription(
        key="model_error",
        translation_key="model_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        value=lambda m: None if _st(m).mean_error is None else round(100 * _st(m).mean_error, 1),
    ),
    PelletsSensorDescription(
        key="journal",
        translation_key="journal",
        value=lambda m: m.describe(max(m.entries, key=lambda e: e["ts"])) if m.entries else None,
        attrs=lambda m: {"entrees": m.journal(JOURNAL_ATTR_SIZE), "alertes": _st(m).warnings[-5:]},
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: Any, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    manager: PelletsManager = entry.runtime_data
    async_add_entities(PelletsSensor(manager, desc) for desc in SENSORS)


def device_info(manager: PelletsManager) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, manager.entry.entry_id)},
        name="Pellets",
        manufacturer="perso",
        model="Gestion pellets",
    )


class PelletsEntity:
    """Mixin: dispatcher wiring shared by sensors and binary sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def _setup(self, manager: PelletsManager, key: str) -> None:
        self._manager = manager
        self._attr_unique_id = f"{manager.entry.entry_id}_{key}"
        self._attr_device_info = device_info(manager)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(  # type: ignore[attr-defined]
            async_dispatcher_connect(
                self.hass,  # type: ignore[attr-defined]
                SIGNAL_UPDATE.format(self._manager.entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()  # type: ignore[attr-defined]


class PelletsSensor(PelletsEntity, SensorEntity):
    entity_description: PelletsSensorDescription

    def __init__(self, manager: PelletsManager, description: PelletsSensorDescription) -> None:
        self.entity_description = description
        self._setup(manager, description.key)

    @property
    def native_value(self) -> Any:
        if self._manager.state is None:
            return None
        return self.entity_description.value(self._manager)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._manager.state is None or self.entity_description.attrs is None:
            return None
        return self.entity_description.attrs(self._manager)
