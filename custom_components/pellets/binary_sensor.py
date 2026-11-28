"""Pellets binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_HOME_LOW_BAGS, CONF_LOW_KG
from .manager import PelletsManager
from .sensor import PelletsEntity


@dataclass(frozen=True, kw_only=True)
class PelletsBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[PelletsManager], bool | None]


def _reservoir_low(m: PelletsManager) -> bool | None:
    kg = m.state.reservoir_kg if m.state else None
    return None if kg is None else kg <= float(m.conf(CONF_LOW_KG))


def _full_bag_fits(m: PelletsManager) -> bool | None:
    """A whole bag fits even if the model is wrong by 2 sigma (reservoir underestimated)."""
    free = m.derived.get("free_kg")
    if free is None:
        return None
    sigma = (m.state.reservoir_sigma_kg or 0.0) if m.state else 0.0
    return free - 2 * sigma >= m.params.bag_kg


BINARY: tuple[PelletsBinaryDescription, ...] = (
    PelletsBinaryDescription(
        key="reservoir_low", translation_key="reservoir_low",
        device_class=BinarySensorDeviceClass.PROBLEM, value=_reservoir_low,
    ),
    PelletsBinaryDescription(
        key="home_stock_low", translation_key="home_stock_low",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value=lambda m: None if m.state is None else m.state.home_bags <= float(m.conf(CONF_HOME_LOW_BAGS)),
    ),
    PelletsBinaryDescription(key="full_bag_fits", translation_key="full_bag_fits", value=_full_bag_fits),
    PelletsBinaryDescription(
        key="burning", translation_key="burning", device_class=BinarySensorDeviceClass.RUNNING,
        value=lambda m: bool(m.derived.get("power_level")),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: Any, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    manager: PelletsManager = entry.runtime_data
    async_add_entities(PelletsBinarySensor(manager, desc) for desc in BINARY)


class PelletsBinarySensor(PelletsEntity, BinarySensorEntity):
    entity_description: PelletsBinaryDescription

    def __init__(self, manager: PelletsManager, description: PelletsBinaryDescription) -> None:
        self.entity_description = description
        self._setup(manager, description.key)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self._manager)
