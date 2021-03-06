"""Plugwise platform for Home Assistant Core."""

import asyncio
from datetime import timedelta
import logging
from typing import Dict

from Plugwise_Smile.Smile import Smile
import async_timeout
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    ALL_PLATFORMS,
    COORDINATOR,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SENSOR_PLATFORMS,
    UNDO_UPDATE_LISTENER,
)

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry_gw(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Plugwise Smiles from a config entry."""
    websession = async_get_clientsession(hass, verify_ssl=False)

    api = Smile(
        host=entry.data[CONF_HOST],
        password=entry.data[CONF_PASSWORD],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        timeout=30,
        websession=websession,
    )

    try:
        connected = await api.connect()

        if not connected:
            _LOGGER.error("Unable to connect to Smile")
            raise ConfigEntryNotReady

    except Smile.InvalidAuthentication:
        _LOGGER.error("Invalid Smile ID")
        return False

    except Smile.PlugwiseError as err:
        _LOGGER.error("Error while communicating to device")
        raise ConfigEntryNotReady from err

    except asyncio.TimeoutError as err:
        _LOGGER.error("Timeout while connecting to Smile")
        raise ConfigEntryNotReady from err

    update_interval = timedelta(
        seconds=entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL[api.smile_type]
        )
    )

    async def async_update_data():
        """Update data via API endpoint."""
        try:
            async with async_timeout.timeout(10):
                await api.full_update_device()
                return True
        except Smile.XMLDataMissingError as err:
            raise UpdateFailed("Smile update failed") from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Smile",
        update_method=async_update_data,
        update_interval=update_interval,
    )

    await coordinator.async_refresh()

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    api.get_all_devices()

    if entry.unique_id is None:
        if api.smile_version[0] != "1.8.0":
            hass.config_entries.async_update_entry(entry, unique_id=api.smile_hostname)

    undo_listener = entry.add_update_listener(_update_listener)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        COORDINATOR: coordinator,
        UNDO_UPDATE_LISTENER: undo_listener,
    }

    device_registry = await dr.async_get_registry(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, api.gateway_id)},
        manufacturer="Plugwise",
        name=entry.title,
        model=f"Smile {api.smile_name}",
        sw_version=api.smile_version[0],
    )

    single_master_thermostat = api.single_master_thermostat()

    platforms = ALL_PLATFORMS
    if single_master_thermostat is None:
        platforms = SENSOR_PLATFORMS

    for component in platforms:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )

    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    coordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR]
    coordinator.update_interval = timedelta(
        seconds=entry.options.get(CONF_SCAN_INTERVAL)
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in ALL_PLATFORMS
            ]
        )
    )

    hass.data[DOMAIN][entry.entry_id][UNDO_UPDATE_LISTENER]()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class SmileGateway(CoordinatorEntity):
    """Represent Smile Gateway."""

    def __init__(self, api, coordinator, name, dev_id):
        """Initialise the gateway."""
        super().__init__(coordinator)

        self._api = api
        self._name = name
        self._dev_id = dev_id

        self._unique_id = None
        self._model = None

        self._entity_name = self._name

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the entity, if any."""
        return self._name

    @property
    def device_info(self) -> Dict[str, any]:
        """Return the device information."""

        device_information = {
            "identifiers": {(DOMAIN, self._dev_id)},
            "name": self._entity_name,
            "manufacturer": "Plugwise",
        }

        if self._model is not None:
            device_information["model"] = self._model.replace("_", " ").title()

        if self._dev_id != self._api.gateway_id:
            device_information["via_device"] = (DOMAIN, self._api.gateway_id)

        return device_information

    async def async_added_to_hass(self):
        """Subscribe to updates."""
        self._async_process_data()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._async_process_data)
        )

    @callback
    def _async_process_data(self):
        """Interpret and process API data."""
        raise NotImplementedError
