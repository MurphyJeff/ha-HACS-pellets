"""Integration tests running inside a real (test) Home Assistant."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.pellets.const import DOMAIN

ENTITIES = {
    "status_entity": "sensor.poele_status",
    "power_entity": "sensor.poele_real_power",
    "climate_entity": "climate.poele",
    "depleted_entity": "binary_sensor.poele_pellets_depleted",
}
OPTIONS = {"initial_factor": 0.8, "feed_p1": 3.0, "feed_p3": 6.0}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set("sensor.poele_status", "OFF")
    hass.states.async_set("sensor.poele_real_power", "3")
    hass.states.async_set("climate.poele", "heat", {"hvac_action": "idle"})
    hass.states.async_set("binary_sensor.poele_pellets_depleted", "off")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTITIES, options=OPTIONS, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _val(hass, eid):
    return hass.states.get(eid).state


async def test_fill_burn_and_undo(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await _setup(hass)
    await hass.services.async_call(DOMAIN, "purchase", {"bags": 100, "total_eur": 650}, blocking=True)
    await hass.services.async_call(DOMAIN, "transfer", {"bags": 7, "supplier_remaining": 93}, blocking=True)
    await hass.services.async_call(DOMAIN, "fill", {"bags": 1.15, "full": True}, blocking=True)
    await hass.async_block_till_done()
    assert float(_val(hass, "sensor.pellets_reservoir")) == 20.0
    assert float(_val(hass, "sensor.pellets_stock_maison")) == 5.85
    assert float(_val(hass, "sensor.pellets_stock_fournisseur")) == 93
    assert float(_val(hass, "sensor.pellets_prix_moyen_du_sac")) == 6.5

    # Burn 2 h at P3 -> 2 * (0.93 * 6.0 / 3.0) * 0.8 kg (rate from configured auger times)
    hass.states.async_set("sensor.poele_status", "WORKING")
    await hass.async_block_till_done()
    assert _val(hass, "binary_sensor.pellets_combustion_en_cours") == "on"
    freezer.tick(timedelta(hours=2))
    hass.states.async_set("sensor.poele_status", "FINAL CLEANING")
    await hass.async_block_till_done()
    expected = 20 - 2 * (0.93 * 6.0 / 3.0) * 0.8
    assert abs(float(_val(hass, "sensor.pellets_reservoir")) - expected) < 0.06
    burnt = 20 - expected
    assert abs(float(_val(hass, "sensor.pellets_consommation_aujourd_hui")) - burnt) < 0.06
    assert abs(float(_val(hass, "sensor.pellets_temps_de_combustion_aujourd_hui")) - 2) < 0.01
    assert _val(hass, "sensor.pellets_allumages_aujourd_hui") == "1"
    assert abs(float(_val(hass, "sensor.pellets_energie_des_pellets")) - burnt * 4.8) < 0.3
    assert abs(float(_val(hass, "sensor.pellets_derniere_combustion")) - 2) < 0.01
    assert abs(float(_val(hass, "sensor.pellets_cout_aujourd_hui")) - burnt * 6.5 / 15) < 0.05

    # Undo the fill -> reservoir unknown again, stock back to 7
    await hass.services.async_call(DOMAIN, "undo_last", {}, blocking=True)
    await hass.async_block_till_done()
    assert float(_val(hass, "sensor.pellets_stock_maison")) == 7
    assert _val(hass, "sensor.pellets_reservoir") == "unknown"


async def test_depleted_then_stop_records_empty(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await _setup(hass)
    events = []
    hass.bus.async_listen("pellets_event", events.append)
    await hass.services.async_call(DOMAIN, "fill", {"bags": 1, "full": True}, blocking=True)
    hass.states.async_set("sensor.poele_status", "WORKING")
    await hass.async_block_till_done()
    freezer.tick(timedelta(hours=8))
    hass.states.async_set("binary_sensor.poele_pellets_depleted", "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=20))
    hass.states.async_set("sensor.poele_status", "OFF")
    await hass.async_block_till_done()
    assert any(e.data["type"] == "empty_detected" for e in events)
    assert float(_val(hass, "sensor.pellets_reservoir")) > 0  # nothing recorded without confirmation
    await hass.services.async_call(DOMAIN, "mark_empty", {}, blocking=True)  # user confirms
    await hass.async_block_till_done()
    assert float(_val(hass, "sensor.pellets_reservoir")) == 0
    attrs = hass.states.get("sensor.pellets_facteur_de_calibration").attributes
    assert attrs["calibrations"] == 1


async def test_options_flow_edit_entry(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await hass.services.async_call(DOMAIN, "transfer", {"bags": 7}, blocking=True)
    mgr = entry.runtime_data
    entry_id = mgr.entries[0]["id"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "journal"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"entry": entry_id})
    assert result["step_id"] == "edit"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"when": "2026-11-12 10:00:00", "bags": 6, "delete": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert float(_val(hass, "sensor.pellets_stock_maison")) == 6


async def test_entity_ids_used_by_dashboard(hass: HomeAssistant) -> None:
    import pathlib
    import re

    await _setup(hass)
    view = (pathlib.Path(__file__).parents[1] / "dashboard" / "pellets_view.yaml").read_text()
    for eid in set(re.findall(r"\b(?:sensor|binary_sensor)\.pellets_[a-z_]+", view)):
        assert hass.states.get(eid) is not None, eid


async def test_setup_wizard_guesses_entities_and_seeds_journal(hass: HomeAssistant) -> None:
    for eid, state in ENTITIES.items():
        hass.states.async_set(state, "off")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    assert result["description_placeholders"]["found"] == "4"
    schema = {str(k): k.description["suggested_value"] for k in result["data_schema"].schema}
    assert schema["power_entity"] == "sensor.poele_real_power"
    assert schema["depleted_entity"] == "binary_sensor.poele_pellets_depleted"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTITIES)
    assert result["step_id"] == "model"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"capacity_kg": 20, "bag_kg": 15, "initial_factor": 1.0, "reservoir_low_kg": 5, "home_low_bags": 3, "feed_p1": 3.0},
    )
    assert result["step_id"] == "start"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"home_bags": 6.5, "supplier_bags": 40, "price_per_bag": 6.5, "reservoir_level": "0.5", "history_days": 0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["feed_p1"] == 3.0
    await hass.async_block_till_done()

    assert float(_val(hass, "sensor.pellets_stock_maison")) == 6.5
    assert float(_val(hass, "sensor.pellets_stock_fournisseur")) == 40
    assert float(_val(hass, "sensor.pellets_reservoir")) == 10
    assert float(_val(hass, "sensor.pellets_prix_moyen_du_sac")) == 6.5

    # Reloading must not seed the journal twice.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(entry.runtime_data.entries) == 3
