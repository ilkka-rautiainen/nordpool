import asyncio
import logging
from collections import defaultdict
from datetime import timedelta


import backoff
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_REGION, Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_utils

from .aio_price import AioPrices, InvalidValueException, RETRYABLE_PRICE_ERRORS
from .events import async_track_time_change_in_tz
from .services import async_setup_services

from .const import (
    NAME,
    VERSION,
    ISSUEURL,
    DOMAIN,
    DEFAULT_REGION,
    EVENT_NEW_DAY,
    EVENT_NEW_HOUR,
    EVENT_NEW_PRICE,
    _CURRENCY_LIST,
    _REGIONS,
    RANDOM_MINUTE,
    RANDOM_SECOND,
)


STARTUP = f"""
-------------------------------------------------------------------
{NAME}
Version: {VERSION}
This is a custom component
If you have any issues with this you need to open an issue here:
{ISSUEURL}
-------------------------------------------------------------------
"""

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


class NordpoolData:
    """Holds the data"""

    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._data = defaultdict(dict)
        self._areas = defaultdict(set)
        self._refresh_lock = asyncio.Lock()

    async def _refresh(self, currency, day):
        client = async_get_clientsession(self._hass)
        date = dt_utils.now()
        if day == "tomorrow":
            date += timedelta(hours=24)

        prices = await AioPrices(currency, client).hourly(
            end_date=date, areas=sorted(self._areas[currency])
        )
        if not prices:
            raise InvalidValueException(f"No Nord Pool prices for {currency} {day}")
        return prices["areas"]

    async def update_tomorrow(self):
        """Fetch tomorrow's prices for all configured currencies."""
        async with self._refresh_lock:
            for currency in self._areas:
                self._data[currency]["tomorrow"] = await self._refresh(
                    currency, "tomorrow"
                )

    async def rollover(self):
        """Use tomorrow's prices as today's at midnight, when available."""
        async with self._refresh_lock:
            for days in self._data.values():
                days["today"] = days.pop("tomorrow", {})

    async def _someday(self, area: str, currency: str, day: str):
        """Return prices for an area, fetching again if the cache is empty."""
        if currency not in _CURRENCY_LIST:
            raise ValueError(
                "%s is an invalid currency, possible values are %s"
                % (currency, ", ".join(_CURRENCY_LIST))
            )

        prices = self._data.get(currency, {}).get(day, {}).get(area)
        if prices:
            return prices

        async with self._refresh_lock:
            self._areas[currency].add(area)
            days = self._data[currency]
            if not days.get(day, {}).get(area):
                days[day] = await self._refresh(currency, day)
            prices = days[day].get(area)
            if not prices:
                raise InvalidValueException(f"No Nord Pool prices for {area} {day}")
            return prices

    async def today(self, area: str, currency: str) -> dict:
        """Returns today's prices in an area in the requested currency"""
        return await self._someday(area, currency, "today")

    async def tomorrow(self, area: str, currency: str):
        """Returns tomorrow's prices in an area in the requested currency"""
        return await self._someday(area, currency, "tomorrow")


async def _dry_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up using yaml config file."""
    if DOMAIN not in hass.data:
        api = NordpoolData(hass)
        hass.data[DOMAIN] = api
        _LOGGER.debug("Added %s to hass.data", DOMAIN)
        await async_setup_services(hass)

        async def new_day_cb(_):
            """Cb to handle some house keeping when it a new day."""
            _LOGGER.debug("Called new_day_cb callback")

            await api.rollover()

            async_dispatcher_send(hass, EVENT_NEW_DAY)

        async def new_hr(_):
            """Callback to tell the sensors to update on a new hour."""
            _LOGGER.debug("Called new_hr callback")
            async_dispatcher_send(hass, EVENT_NEW_HOUR)

        @backoff.on_exception(
            backoff.constant,
            RETRYABLE_PRICE_ERRORS,
            logger=_LOGGER,
            interval=600,
            max_time=7200,
            jitter=None,
        )
        async def new_data_cb(_):
            """Callback to fetch new data for tomorrows prices at 1300ish CET
            and notify any sensors, about the new data
            """
            # _LOGGER.debug("Called new_data_cb")
            await api.update_tomorrow()
            async_dispatcher_send(hass, EVENT_NEW_PRICE)

        # Handles futures updates
        async_track_time_change_in_tz(
            hass,
            new_data_cb,
            hour=13,
            minute=RANDOM_MINUTE,
            second=RANDOM_SECOND,
            tz=await dt_utils.async_get_time_zone("Europe/Stockholm"),
        )

        async_track_time_change(
            hass, new_day_cb, hour=0, minute=0, second=0
        )

        async_track_time_change(hass, new_hr, minute=[0, 15, 30, 45], second=0)

    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up using yaml config file."""
    return await _dry_setup(hass, config)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up nordpool as config entry."""
    await _dry_setup(hass, entry.data)
    area = entry.data.get(CONF_REGION, DEFAULT_REGION)
    currency = entry.data.get("currency") or _REGIONS[area][0]
    try:
        await hass.data[DOMAIN].today(area, currency)
    except RETRYABLE_PRICE_ERRORS as err:
        raise ConfigEntryNotReady(f"Could not fetch Nord Pool prices: {err}") from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload only this entry; YAML and other entries share the cache and timers."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload through Home Assistant so setup and retries are handled normally."""
    await hass.config_entries.async_reload(entry.entry_id)
