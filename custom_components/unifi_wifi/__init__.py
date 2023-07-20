"""The Unifi Wifi integration."""

import logging, aiohttp, asyncio
import voluptuous as vol

from datetime import datetime
from homeassistant.const import (
    CONF_ENABLED,
    CONF_METHOD,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify
from .const import (
    DOMAIN,
    CONF_BASEURL,
    CONF_BASEURL_REGEX,
    CONF_CHAR_COUNT,
    CONF_CONTROLLER_NAME,
    CONF_DELIMITER,
    CONF_DELIMITER_TYPES,
    CONF_MAX_LENGTH,
    CONF_METHOD_TYPES,
    CONF_MIN_LENGTH,
    CONF_MONITORED_SSIDS,
    CONF_SITE,
    CONF_SSID,
    CONF_UNIFI_OS,
    CONF_WORD_COUNT,
    SERVICE_CUSTOM_PASSWORD,
    SERVICE_RANDOM_PASSWORD,
    SERVICE_ENABLE_WLAN,
    UNIFI_NAME,
    UNIFI_PASSWORD
)
from . import unifi
from . import password as pw


_LOGGER = logging.getLogger(__name__)


_SSID_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
})

_SITE_SCHEMA = vol.Schema({
    vol.Required(CONF_CONTROLLER_NAME): cv.string,
    vol.Required(CONF_SITE): cv.string,
    vol.Required(CONF_BASEURL): cv.matches_regex(CONF_BASEURL_REGEX),
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional(CONF_SCAN_INTERVAL, default=600): cv.time_period,
    vol.Optional(CONF_UNIFI_OS, default=True): cv.boolean,
    vol.Optional(CONF_VERIFY_SSL, default=False): cv.boolean,
    vol.Optional(CONF_MONITORED_SSIDS, default=[]): vol.All(
        cv.ensure_list, [_SSID_SCHEMA]
    ),
})

def _unique_controller_names(obj):
    names = [slugify(conf[CONF_CONTROLLER_NAME]) for conf in obj]
    msg = f"Duplicate controller_name values are not allowed: {names}"
    vol.Unique(msg)(names)
    return obj

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.All(
        cv.ensure_list, [_SITE_SCHEMA], _unique_controller_names,
    )},
    extra=vol.ALLOW_EXTRA,
)

SERVICE_CUSTOM_PASSWORD_SCHEMA = vol.Schema({
    vol.Required(CONF_CONTROLLER_NAME): cv.string,
    vol.Required(CONF_SSID): cv.string,
    vol.Required(CONF_PASSWORD): vol.All(
        cv.string, vol.Length(min=8, max=63)
    ),
})

def _check_word_lengths(obj):
    if obj[CONF_MIN_LENGTH] > obj[CONF_MAX_LENGTH]:
        msg = f"{CONF_MIN_LENGTH} ({obj[CONF_MIN_LENGTH]}) must be less than or equal to {CONF_MAX_LENGTH} ({obj[CONF_MAX_LENGTH]})"
        raise vol.Invalid(msg)
    return obj

SERVICE_RANDOM_PASSWORD_SCHEMA = vol.All(
    vol.Schema({
        vol.Required(CONF_CONTROLLER_NAME): cv.string,
        vol.Required(CONF_SSID): cv.string,
        vol.Required(CONF_METHOD): vol.In(CONF_METHOD_TYPES),
        vol.Optional(CONF_DELIMITER, default='space'): vol.In(CONF_DELIMITER_TYPES),
        vol.Optional(CONF_MIN_LENGTH, default=5): vol.All(
            vol.Coerce(int), vol.Range(min=3, max=9)
        ),
        vol.Optional(CONF_MAX_LENGTH, default=8): vol.All(
            vol.Coerce(int), vol.Range(min=3, max=9)
        ),
        vol.Optional(CONF_WORD_COUNT, default=4): vol.All(
            vol.Coerce(int), vol.Range(min=3, max=6)
        ),
        vol.Optional(CONF_CHAR_COUNT, default=24): vol.All(
            vol.Coerce(int), vol.Range(min=8, max=63)
        ),
    }),
    _check_word_lengths
)

SERVICE_ENABLE_WLAN_SCHEMA = vol.Schema({
    vol.Required(CONF_CONTROLLER_NAME): cv.string,
    vol.Required(CONF_SSID): cv.string,
    vol.Required(CONF_ENABLED): cv.boolean,
})

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:

    # define individual controllers (DataUpdateCoordinators)
    coordinators = [unifi.UnifiWifiController(hass, conf) for conf in config[DOMAIN]]
    hass.data[DOMAIN] = config[DOMAIN]

    # create image entities (CoordinatorEntities)
    hass.async_create_task(async_load_platform(hass, 'image', DOMAIN, coordinators, config))


    def _controller_index(controller):
        """Find the array index of a specific controller within the DataUpdateCoordinators."""
        for x in coordinators:
            if x.controller_name == controller:
                return coordinators.index(x)
        # ELSE
        raise ValueError(f"The controller {controller} is not configured in YAML")

    def _validate_ssid(controller, ssid) -> bool:
        """ Check if an ssid exists on specified controller."""
        ind = _controller_index(controller)

        # https://github.com/home-assistant/core/blob/dev/homeassistant/core.py#L423
        # NOT SURE IF this should be async_refresh() or async_request_refresh()
        hass.add_job(coordinators[ind].async_request_refresh())

        for x in coordinators[ind].wlanconf:
            if x[UNIFI_NAME] == ssid:
                return True
        # ELSE
        raise ValueError(f"The SSID {ssid} does not exist on controller {controller}")
        return False

    async def custom_password_service(call):
        """Set a custom password."""
        controller = call.data.get(CONF_CONTROLLER_NAME)
        ssid = call.data.get(CONF_SSID)
        password = call.data.get(CONF_PASSWORD)

        ind = _controller_index(controller)
        valid = _validate_ssid(controller, ssid)
        # password is already validated as a string in SERVICE_RANDOM_PASSWORD_SCHEMA
        # should it be further validated as alphanumeric?
        #    https://docs.python.org/3/library/stdtypes.html#str.isalnum

        if valid:
            payload = {UNIFI_PASSWORD: password}
            await coordinators[ind].set_wlanconf(ssid, payload)

    async def random_password_service(call):
        """Set a randomized password."""
        controller = call.data.get(CONF_CONTROLLER_NAME)
        ssid = call.data.get(CONF_SSID)
        method = call.data.get(CONF_METHOD)
        _delimiter = call.data.get(CONF_DELIMITER)
        min_length = call.data.get(CONF_MIN_LENGTH)
        max_length = call.data.get(CONF_MAX_LENGTH)
        word_count = call.data.get(CONF_WORD_COUNT)
        char_count = call.data.get(CONF_CHAR_COUNT)

        ind = _controller_index(controller)
        _validate_ssid(controller, ssid)

        if _delimiter == 'space':
            delimiter = ' '
        elif _delimiter == 'dash':
            delimiter = '-'
        elif _delimiter == 'none':
            delimiter = ''
        else:
            raise ValueError(f"invalid delimiter option ({_delimiter})")
        
        password = await hass.async_add_executor_job(pw.create, method, delimiter, min_length, max_length, word_count, char_count)

        payload = {UNIFI_PASSWORD: password}
        _LOGGER.debug("Payload is %s", payload)
        # await coordinators[ind].set_wlanconf(ssid, payload)

    async def enable_wlan_service(call):
        controller = call.data.get(CONF_CONTROLLER_NAME)
        ssid = call.data.get(CONF_SSID)
        enabled = call.data.get(CONF_ENABLED)

        ind = _controller_index(controller)
        _validate_ssid(controller, ssid)

        payload = {'enabled': str(enabled).lower()}
        await coordinators[ind].set_wlanconf(ssid, payload)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CUSTOM_PASSWORD,
        custom_password_service,
        schema=SERVICE_CUSTOM_PASSWORD_SCHEMA
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RANDOM_PASSWORD,
        random_password_service,
        schema=SERVICE_RANDOM_PASSWORD_SCHEMA
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ENABLE_WLAN,
        enable_wlan_service,
        schema=SERVICE_ENABLE_WLAN_SCHEMA
    )

    return True