"""Constants for the Pellets integration."""

DOMAIN = "pellets"
EVENT = "pellets_event"
STORAGE_VERSION = 1
SIGNAL_UPDATE = "pellets_update_{}"

CONF_STATUS_ENTITY = "status_entity"
CONF_POWER_ENTITY = "power_entity"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_DEPLETED_ENTITY = "depleted_entity"
CONF_CAPACITY = "capacity_kg"
CONF_BAG_KG = "bag_kg"
CONF_INITIAL_FACTOR = "initial_factor"
CONF_FEED = "feed_p{}"
CONF_LOW_KG = "reservoir_low_kg"
CONF_HOME_LOW_BAGS = "home_low_bags"

DEFAULTS = {
    CONF_CAPACITY: 20.0,
    CONF_BAG_KG: 15.0,
    CONF_INITIAL_FACTOR: 1.0,
    CONF_LOW_KG: 5.0,
    CONF_HOME_LOW_BAGS: 3.0,
}

BURNING_STATUSES = ("WORKING",)
DEPLETED_CONFIRM_S = 3600
POSSIBLE_EMPTY_MARGIN_KG = 3.0
JOURNAL_ATTR_SIZE = 15
