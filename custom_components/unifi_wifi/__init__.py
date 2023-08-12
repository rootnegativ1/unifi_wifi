"""The Unifi Wifi integration."""

import logging, aiohttp, asyncio
import voluptuous as vol

from datetime import datetime
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.const import (
    CONF_ENABLED,
    CONF_HOST,
    CONF_MAC,
    CONF_METHOD,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    CONF_VERIFY_SSL
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify
from .const import (
    DOMAIN,
    CONF_CHAR_COUNT,
    CONF_DELIMITER,
    CONF_DELIMITER_TYPES,
    CONF_FORCE_PROVISION,
    CONF_MANAGED_APS,
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

_AP_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_MAC): cv.string
})

_SSID_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
})

_SITE_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional(CONF_SITE, default='default'): cv.string,
    vol.Optional(CONF_PORT, default=443): cv.port,
    vol.Optional(CONF_SCAN_INTERVAL, default=600): cv.time_period,
    vol.Optional(CONF_UNIFI_OS, default=True): cv.boolean,
    vol.Optional(CONF_VERIFY_SSL, default=False): cv.boolean,
    vol.Optional(CONF_FORCE_PROVISION, default=False): cv.boolean,
    vol.Optional(CONF_MANAGED_APS, default=[]): vol.All(
        cv.ensure_list, [_AP_SCHEMA]
    ),
    vol.Optional(CONF_MONITORED_SSIDS, default=[]): vol.All(
        cv.ensure_list, [_SSID_SCHEMA]
    ),
})

def _unique_names(obj):
    names = [slugify(conf[CONF_NAME]) for conf in obj]
    msg = f"Duplicate name values are not allowed: {names}"
    vol.Unique(msg)(names)
    return obj

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.All(
        cv.ensure_list, [_SITE_SCHEMA], _unique_names,
    )},
    extra=vol.ALLOW_EXTRA,
)

def _is_ascii(obj):
    # password is already validated as a string in SERVICE_CUSTOM_PASSWORD_SCHEMA
    # should it be further validated as ascii?
    #    https://stackoverflow.com/questions/196345/how-to-check-if-a-string-in-python-is-in-ascii
    #    https://docs.python.org/3/library/stdtypes.html#str.isascii
    s = obj[CONF_PASSWORD]
    if not s.isascii():
        raise ValueError("Password may only contain ASCII characters.")
    return obj

SERVICE_CUSTOM_PASSWORD_SCHEMA = vol.All(
    vol.Schema({
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_SSID): cv.string,
        vol.Required(CONF_PASSWORD): vol.All(
            cv.string, vol.Length(min=8, max=63)
        ),
    }),
    _is_ascii
)

def _check_word_lengths(obj):
    if obj[CONF_MIN_LENGTH] > obj[CONF_MAX_LENGTH]:
        msg = f"{CONF_MIN_LENGTH} ({obj[CONF_MIN_LENGTH]}) must be less than or equal to {CONF_MAX_LENGTH} ({obj[CONF_MAX_LENGTH]})"
        raise vol.Invalid(msg)
    return obj

SERVICE_RANDOM_PASSWORD_SCHEMA = vol.All(
    vol.Schema({
        vol.Required(CONF_NAME): cv.string,
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
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_SSID): cv.string,
    vol.Required(CONF_ENABLED): cv.boolean,
})

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:

    coordinators = [unifi.UnifiWifiCoordinator(hass, conf) for conf in config[DOMAIN]]
    hass.data[DOMAIN] = config[DOMAIN]

    hass.async_create_task(async_load_platform(hass, 'image', DOMAIN, coordinators, config))


    def _coordinator_index(name):
        """Find the array index of a specific coordinator."""
        for x in coordinators:
            if x.name == name:
                return coordinators.index(x)
        raise ValueError(f"The coordinator {name} is not configured in YAML")

    def _validate_ssid(ind, ssid) -> bool:
        """ Check if an ssid exists on specified coordinator."""
        # https://github.com/home-assistant/core/blob/dev/homeassistant/core.py#L423
        # NOT SURE IF this should be async_refresh() or async_request_refresh()
        # I THINK it should be async_request_refresh() so that (near) simultaneous
        #    service calls don't bombard the api on a unifi controller
        hass.add_job(coordinators[ind].async_request_refresh())

        for x in coordinators[ind].wlanconf:
            if x[UNIFI_NAME] == ssid:
                return True
        raise ValueError(f"The SSID {ssid} does not exist on coordinator {coordinators[ind].name}")
        return False

    async def custom_password_service(call):
        """Set a custom password."""
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context, permission=POLICY_CONTROL)
            if not user.is_admin:
                raise Unauthorized()

        coordinator = call.data.get(CONF_NAME)
        ssid = call.data.get(CONF_SSID)
        password = call.data.get(CONF_PASSWORD)

        ind = _coordinator_index(coordinator)
        if _validate_ssid(ind, ssid):
            payload = {UNIFI_PASSWORD: password}
            await coordinators[ind].set_wlanconf(ssid, payload)

    async def random_password_service(call):
        """Set a randomized password."""
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context, permission=POLICY_CONTROL)
            if not user.is_admin:
                raise Unauthorized()

        coordinator = call.data.get(CONF_NAME)
        ssid = call.data.get(CONF_SSID)
        method = call.data.get(CONF_METHOD)
        delimiter_raw = call.data.get(CONF_DELIMITER)
        min_length = call.data.get(CONF_MIN_LENGTH)
        max_length = call.data.get(CONF_MAX_LENGTH)
        word_count = call.data.get(CONF_WORD_COUNT)
        char_count = call.data.get(CONF_CHAR_COUNT)

        if delimiter_raw == 'space':
            delimiter = ' '
        elif delimiter_raw == 'dash':
            delimiter = '-'
        elif delimiter_raw == 'none':
            delimiter = ''
        else:
            raise ValueError(f"invalid delimiter option ({delimiter_raw})")

        ind = _coordinator_index(coordinator)
        if _validate_ssid(ind, ssid):
            password = await hass.async_add_executor_job(pw.create, method, delimiter, min_length, max_length, word_count, char_count)
            payload = {UNIFI_PASSWORD: password}
            await coordinators[ind].set_wlanconf(ssid, payload)

    async def enable_wlan_service(call):
        """Enable or disable a specifed wlan."""
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context, permission=POLICY_CONTROL)
            if not user.is_admin:
                raise Unauthorized()

        coordinator = call.data.get(CONF_NAME)
        ssid = call.data.get(CONF_SSID)
        enabled = call.data.get(CONF_ENABLED)

        ind = _coordinator_index(coordinator)
        if _validate_ssid(coordinator, ssid):
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