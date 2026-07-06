"""OpenIPC integration for Home Assistant."""
import asyncio
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .api_ha import async_register_api
from .const import DOMAIN
from .coordinator import OpenIPCDataUpdateCoordinator
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional("telegram_bot_token"): cv.string,
                vol.Optional("telegram_chat_id"): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

PLATFORMS = [
    Platform.CAMERA,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
]


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the OpenIPC component from YAML configuration."""
    hass.data.setdefault(DOMAIN, {})

    if DOMAIN in config:
        conf = config[DOMAIN]
        telegram_config = {
            "telegram_bot_token": conf.get("telegram_bot_token"),
            "telegram_chat_id": conf.get("telegram_chat_id"),
        }
        hass.data[DOMAIN]["config"] = telegram_config
        _LOGGER.info(
            "Telegram config loaded from YAML: bot_token=%s, chat_id=%s",
            "set" if telegram_config["telegram_bot_token"] else "missing",
            telegram_config["telegram_chat_id"] or "missing",
        )
    else:
        _LOGGER.debug("No OpenIPC YAML configuration found")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OpenIPC from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = OpenIPCDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await async_register_services(hass)

    await async_register_api(hass)
    _LOGGER.info("OpenIPC API endpoints registered")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = hass.data[DOMAIN].get(entry.entry_id)

    if coordinator and hasattr(coordinator, "qr_scanner") and coordinator.qr_scanner:
        await coordinator.qr_scanner.async_deactivate()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    if not hass.data[DOMAIN] or (
        len(hass.data[DOMAIN]) == 1 and "config" in hass.data[DOMAIN]
    ):
        from .services import async_remove_services

        await async_remove_services(hass)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    from .migration import async_migrate_entry as migrate

    return await migrate(hass, config_entry)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    try:
        device_registry = dr.async_get(hass)
        device_registry.async_clear_config_entry(entry.entry_id)
    except Exception as err:
        _LOGGER.debug("Error removing device registry entry: %s", err)


async def async_get_cameras(hass: HomeAssistant) -> list:
    """Get all OpenIPC cameras."""
    cameras = []

    for entry_id, coordinator in hass.data[DOMAIN].items():
        if entry_id == "config":
            continue

        if hasattr(coordinator, "host"):
            model = "Unknown"
            firmware = "Unknown"
            if coordinator.data and "parsed" in coordinator.data:
                model = coordinator.data["parsed"].get("model", "Unknown")
                firmware = coordinator.data["parsed"].get("firmware", "Unknown")

            camera_data = {
                "entry_id": entry_id,
                "name": coordinator.entry.data.get("name", "OpenIPC Camera"),
                "ip": coordinator.host,
                "port": coordinator.port,
                "username": coordinator.username,
                "password": coordinator.password,
                "device_type": coordinator.entry.data.get("device_type", "openipc"),
                "rtsp_port": coordinator.rtsp_port,
                "available": coordinator.data.get("available", False)
                if coordinator.data
                else False,
                "model": model,
                "firmware": firmware,
            }

            if hasattr(coordinator, "beward") and coordinator.beward:
                camera_data["beward"] = {
                    "relay_count": getattr(coordinator.beward, "relay_count", 1),
                    "model": getattr(coordinator.beward, "_model", "DS07P-LP"),
                }

            if hasattr(coordinator, "vivotek") and coordinator.vivotek:
                camera_data["vivotek"] = {
                    "ptz_available": getattr(coordinator.vivotek, "ptz_available", False),
                    "model": getattr(coordinator.vivotek, "model_name", "SD9364-EHL"),
                }

            cameras.append(camera_data)

    return cameras
