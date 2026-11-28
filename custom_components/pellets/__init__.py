"""Pellets: stock, reservoir estimate and self-calibrating consumption for a pellet stove."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import model
from .const import CONF_START, DOMAIN
from .manager import PelletsManager

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type PelletsConfigEntry = ConfigEntry[PelletsManager]

BAGS = vol.All(vol.Coerce(float), vol.Range(min=0, max=1000))
BASE = {vol.Optional("when"): cv.datetime}

SERVICES: dict[str, vol.Schema] = {
    "fill": vol.Schema(
        {vol.Required("bags"): BAGS, vol.Optional("full", default=False): cv.boolean,
         vol.Optional("measured", default=True): cv.boolean, **BASE}
    ),
    "mark_empty": vol.Schema(BASE),
    "purchase": vol.Schema(
        {
            vol.Required("bags"): BAGS,
            vol.Optional("price_per_bag"): vol.Coerce(float),
            vol.Optional("total_eur"): vol.Coerce(float),
            vol.Optional("location", default=model.LOCATION_SUPPLIER): vol.In(
                [model.LOCATION_SUPPLIER, model.LOCATION_HOME]
            ),
            vol.Optional("supplier"): cv.string,
            vol.Optional("brand"): cv.string,
            **BASE,
        }
    ),
    "transfer": vol.Schema(
        {vol.Required("bags"): BAGS, vol.Optional("supplier_remaining"): BAGS, **BASE}
    ),
    "inventory": vol.Schema(
        {
            vol.Optional("home_bags"): BAGS,
            vol.Optional("supplier_bags"): BAGS,
            vol.Optional("reservoir_kg"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            **BASE,
        }
    ),
    "maintenance": vol.Schema(
        {
            vol.Required("kind"): vol.In(model.MAINTENANCE_KINDS),
            vol.Optional("cost_eur"): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional("note"): cv.string,
            **BASE,
        }
    ),
    "undo_last": vol.Schema({}),
    "delete_entry": vol.Schema({vol.Required("entry_id"): cv.string}),
    "edit_entry": vol.Schema(
        {
            vol.Required("entry_id"): cv.string,
            vol.Optional("when"): cv.datetime,
            vol.Optional("bags"): BAGS,
            vol.Optional("full"): cv.boolean,
            vol.Optional("measured"): cv.boolean,
            vol.Optional("price_per_bag"): vol.Coerce(float),
            vol.Optional("total_eur"): vol.Coerce(float),
            vol.Optional("supplier_remaining"): BAGS,
            vol.Optional("home_bags"): BAGS,
            vol.Optional("supplier_bags"): BAGS,
            vol.Optional("reservoir_kg"): vol.Coerce(float),
        }
    ),
    "import_history": vol.Schema({vol.Optional("days", default=10): vol.All(int, vol.Range(min=1, max=60))}),
    "get_journal": vol.Schema({vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=1000))}),
}


def _ts(call: ServiceCall) -> float | None:
    when = call.data.get("when")
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.get_default_time_zone())
    return when.timestamp()


def _manager(hass: HomeAssistant) -> PelletsManager:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Intégration Pellets non chargée")
    return entries[0].runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def handle(call: ServiceCall) -> ServiceResponse:
        mgr = _manager(hass)
        name, data = call.service, dict(call.data)
        data.pop("when", None)
        try:
            if name == "fill":
                entry = mgr.add_entry(model.ENTRY_FILL, data, _ts(call))
            elif name == "mark_empty":
                entry = mgr.add_entry(model.ENTRY_EMPTY, {}, _ts(call))
            elif name == "purchase":
                entry = mgr.add_entry(model.ENTRY_PURCHASE, data, _ts(call))
            elif name == "transfer":
                entry = mgr.add_entry(model.ENTRY_TRANSFER, data, _ts(call))
            elif name == "inventory":
                if not data:
                    raise ServiceValidationError("Inventaire vide")
                entry = mgr.add_entry(model.ENTRY_INVENTORY, data, _ts(call))
            elif name == "maintenance":
                entry = mgr.add_entry(model.ENTRY_MAINTENANCE, data, _ts(call))
            elif name == "undo_last":
                entry = mgr.undo_last()
            elif name == "delete_entry":
                entry = mgr.delete_entry(data["entry_id"])
            elif name == "edit_entry":
                entry_id = data.pop("entry_id")
                entry = mgr.edit_entry(entry_id, _ts(call), data)
            elif name == "import_history":
                return {"segments": await mgr.async_import_history(data["days"])}
            else:  # get_journal
                return {"journal": mgr.journal(data["limit"])}
        except KeyError as err:
            raise ServiceValidationError(f"Entrée inconnue : {err}") from err
        return {"entry": entry, "text": mgr.describe(entry) if entry else None}

    for name, schema in SERVICES.items():
        hass.services.async_register(DOMAIN, name, handle, schema, supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PelletsConfigEntry) -> bool:
    manager = PelletsManager(hass, entry)
    await manager.async_load()
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_start()
    if entry.data.get(CONF_START):
        await manager.async_bootstrap(entry.data[CONF_START])
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PelletsConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: PelletsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

