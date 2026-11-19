"""Config and options flows (settings + journal correction from the UI)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.util import dt as dt_util

from . import model
from .const import (
    CONF_BAG_KG,
    CONF_CAPACITY,
    CONF_CLIMATE_ENTITY,
    CONF_DEPLETED_ENTITY,
    CONF_FEED,
    CONF_HOME_LOW_BAGS,
    CONF_INITIAL_FACTOR,
    CONF_LOW_KG,
    CONF_POWER_ENTITY,
    CONF_START,
    CONF_STATUS_ENTITY,
    DEFAULTS,
    DOMAIN,
    RESERVOIR_LEVELS,
)

# Known stove integrations, then entity-id patterns, used to pre-fill the wizard.
STOVE_PLATFORMS = ("aguaiot", "micronova", "palazzetti", "edilkamin", "fumis")
ENTITY_HINTS = {
    CONF_STATUS_ENTITY: ("_status", "_state"),
    CONF_POWER_ENTITY: ("_real_power", "_power_level", "_power"),
    CONF_CLIMATE_ENTITY: ("",),
    CONF_DEPLETED_ENTITY: ("_pellets_depleted", "_depleted", "_pellet"),
}


def guess_entities(hass: HomeAssistant) -> dict[str, str]:
    """Best guess of the stove entities, so the user only has to confirm."""
    registry = er.async_get(hass)
    stove = [e for e in registry.entities.values() if e.platform in STOVE_PLATFORMS]
    candidates = [e.entity_id for e in stove] or list(hass.states.async_entity_ids())
    guess: dict[str, str] = {}
    for key, domain in ENTITY_KEYS.items():
        pool = [eid for eid in candidates if eid.startswith(f"{domain}.")]
        if not stove and key == CONF_CLIMATE_ENTITY:
            pool = [eid for eid in pool if any(w in eid for w in ("poele", "stove", "pellet"))]
        for suffix in ENTITY_HINTS[key]:
            match = next((eid for eid in pool if eid.endswith(suffix) or (suffix and suffix in eid)), None)
            if match:
                guess[key] = match
                break
    return guess

ENTITY_KEYS = {
    CONF_STATUS_ENTITY: "sensor",
    CONF_POWER_ENTITY: "sensor",
    CONF_CLIMATE_ENTITY: "climate",
    CONF_DEPLETED_ENTITY: "binary_sensor",
}


def _num(min_v: float, max_v: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(min=min_v, max=max_v, step=step, mode=selector.NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


REQUIRED_ENTITIES = (CONF_STATUS_ENTITY, CONF_POWER_ENTITY)


def _entities_schema(values: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {}
    for key, domain in ENTITY_KEYS.items():
        marker = vol.Required if key in REQUIRED_ENTITIES else vol.Optional
        fields[marker(key, description={"suggested_value": values.get(key)})] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain=domain)
        )
    return vol.Schema(fields)


def _settings_schema(cur: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_CAPACITY, default=cur[CONF_CAPACITY]): _num(5, 100, 0.5, "kg"),
        vol.Required(CONF_BAG_KG, default=cur[CONF_BAG_KG]): _num(5, 30, 0.5, "kg"),
        vol.Required(CONF_INITIAL_FACTOR, default=cur[CONF_INITIAL_FACTOR]): _num(0.3, 2.0, 0.01),
        vol.Required(CONF_LOW_KG, default=cur[CONF_LOW_KG]): _num(0, 20, 0.5, "kg"),
        vol.Required(CONF_HOME_LOW_BAGS, default=cur[CONF_HOME_LOW_BAGS]): _num(0, 50, 1),
    }
    for level in range(1, 6):
        key = CONF_FEED.format(level)
        fields[vol.Optional(key, description={"suggested_value": cur.get(key)})] = _num(0.5, 12, 0.1, "s")
    return vol.Schema(fields)


class PelletsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._entities: dict[str, Any] = {}
        self._settings: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1/3: the stove entities, pre-filled when they can be detected."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._entities = user_input
            return await self.async_step_model()
        guess = guess_entities(self.hass)
        return self.async_show_form(
            step_id="user",
            data_schema=_entities_schema(guess),
            description_placeholders={"found": str(len(guess))},
        )

    async def async_step_model(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2/3: hopper, bags and (optional) auger times."""
        if user_input is not None:
            self._settings = user_input
            return await self.async_step_start()
        return self.async_show_form(step_id="model", data_schema=_settings_schema(dict(DEFAULTS)))

    async def async_step_start(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 3/3: starting point (stocks, hopper level, history import)."""
        if user_input is not None:
            return self.async_create_entry(
                title="Pellets", data={**self._entities, CONF_START: user_input}, options=self._settings
            )
        schema = vol.Schema(
            {
                vol.Required("home_bags", default=0): _num(0, 500, 0.05, "sacs"),
                vol.Required("supplier_bags", default=0): _num(0, 1000, 1, "sacs"),
                vol.Optional("price_per_bag"): _num(0, 50, 0.01, "€"),
                vol.Required("reservoir_level", default=""): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(RESERVOIR_LEVELS), translation_key="reservoir_level",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required("history_days", default=10): _num(0, 30, 1, "jours"),
            }
        )
        return self.async_show_form(step_id="start", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        return PelletsOptionsFlow()


class PelletsOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._entry_id: str | None = None

    @property
    def _mgr(self) -> Any:
        return self.config_entry.runtime_data

    def _current(self) -> dict[str, Any]:
        return {**DEFAULTS, **self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["journal", "settings", "entities"])

    # ------------------------------------------------------------- settings
    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        return self.async_show_form(step_id="settings", data_schema=_settings_schema(self._current()))

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        return self.async_show_form(step_id="entities", data_schema=_entities_schema(self._current()))

    # -------------------------------------------------------------- journal
    async def async_step_journal(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        mgr = self._mgr
        if user_input is not None:
            self._entry_id = user_input["entry"]
            return await self.async_step_edit()
        options = [
            selector.SelectOptionDict(value=e["id"], label=mgr.describe(e))
            for e in sorted(mgr.entries, key=lambda e: e["ts"], reverse=True)[:100]
        ]
        if not options:
            return self.async_abort(reason="empty_journal")
        return self.async_show_form(
            step_id="journal",
            data_schema=vol.Schema(
                {vol.Required("entry"): selector.SelectSelector(selector.SelectSelectorConfig(options=options))}
            ),
        )

    async def async_step_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        mgr = self._mgr
        entry = next(e for e in mgr.entries if e["id"] == self._entry_id)
        if user_input is not None:
            if user_input.pop("delete", False):
                mgr.delete_entry(entry["id"])
            else:
                when = dt_util.parse_datetime(user_input.pop("when"))
                if when is not None and when.tzinfo is None:
                    when = when.replace(tzinfo=dt_util.get_default_time_zone())
                mgr.edit_entry(entry["id"], when.timestamp() if when else None, user_input)
            return self.async_create_entry(data=dict(self.config_entry.options))

        d = entry["data"]
        local = dt_util.as_local(dt_util.utc_from_timestamp(entry["ts"])).replace(tzinfo=None)
        fields: dict[Any, Any] = {vol.Required("when", default=local.isoformat(sep=" ", timespec="minutes")): selector.DateTimeSelector()}
        kind = entry["type"]
        if kind in (model.ENTRY_FILL, model.ENTRY_PURCHASE, model.ENTRY_TRANSFER):
            fields[vol.Required("bags", default=d.get("bags", 0))] = _num(0, 1000, 0.05, "sacs")
        if kind == model.ENTRY_FILL:
            fields[vol.Required("full", default=bool(d.get("full")))] = selector.BooleanSelector()
        if kind == model.ENTRY_PURCHASE:
            fields[vol.Optional("total_eur", description={"suggested_value": d.get("total_eur")})] = _num(0, 100000, 0.01, "€")
            fields[vol.Optional("price_per_bag", description={"suggested_value": d.get("price_per_bag")})] = _num(0, 100, 0.01, "€")
        if kind == model.ENTRY_TRANSFER:
            fields[vol.Optional("supplier_remaining", description={"suggested_value": d.get("supplier_remaining")})] = _num(0, 10000, 1, "sacs")
        if kind == model.ENTRY_INVENTORY:
            for key, unit in (("home_bags", "sacs"), ("supplier_bags", "sacs"), ("reservoir_kg", "kg")):
                fields[vol.Optional(key, description={"suggested_value": d.get(key)})] = _num(0, 10000, 0.05, unit)
        if kind == model.ENTRY_MAINTENANCE:
            fields[vol.Required("kind", default=d.get("kind", "entretien"))] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(model.MAINTENANCE_KINDS), translation_key="maintenance_kind")
            )
            fields[vol.Optional("cost_eur", description={"suggested_value": d.get("cost_eur")})] = _num(0, 5000, 0.01, "€")
            fields[vol.Optional("note", description={"suggested_value": d.get("note")})] = selector.TextSelector()
        fields[vol.Optional("delete", default=False)] = selector.BooleanSelector()
        return self.async_show_form(
            step_id="edit",
            data_schema=vol.Schema(fields),
            description_placeholders={"entry": mgr.describe(entry)},
        )
