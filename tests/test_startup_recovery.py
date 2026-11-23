"""Regression tests for recovery when Nord Pool is unreachable at startup."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientConnectionError
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.template import Template

from custom_components.nordpool import NordpoolData, async_setup_entry
from custom_components.nordpool.aio_price import InvalidValueException
from custom_components.nordpool.const import DOMAIN
from custom_components.nordpool.sensor import NordpoolSensor

NOW = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)


def price_data():
    midnight = NOW.replace(hour=0)
    return {
        "SE3": {
            "values": [
                {
                    "start": midnight + timedelta(minutes=15 * index),
                    "end": midnight + timedelta(minutes=15 * (index + 1)),
                    "value": 800,
                }
                for index in range(96)
            ]
        }
    }


class NordpoolRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_config_entry_defers_platform_setup_until_prices_are_available(self):
        hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
        )
        api = NordpoolData(hass)
        hass.data[DOMAIN] = api
        api._refresh = AsyncMock(
            side_effect=[ClientConnectionError("DNS lookup failed"), price_data()]
        )
        entry = Mock(data={"region": "SE3", "currency": "SEK"})

        with patch("custom_components.nordpool._dry_setup", new_callable=AsyncMock):
            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)
            hass.config_entries.async_forward_entry_setups.assert_not_awaited()

            self.assertTrue(await async_setup_entry(hass, entry))

        self.assertEqual(api._refresh.await_count, 2)
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        entry.async_on_unload.assert_called_once()

    async def test_yaml_sensor_recovers_on_next_signal_without_reregistering(self):
        hass = Mock()
        api = NordpoolData(hass)
        api._refresh = AsyncMock(
            side_effect=[ClientConnectionError("DNS lookup failed"), price_data()]
        )
        additional_costs = Mock(spec=Template, template="{{ 0 }}")
        additional_costs.async_render.return_value = 0
        sensor = NordpoolSensor(
            "", "SE3", "kWh", 3, 1.0, "SEK", True, False,
            api, additional_costs, hass,
        )
        sensor.hass = hass
        self.addCleanup(setattr, sensor, "hass", None)
        sensor.async_write_ha_state = Mock()

        unsubscribes = [Mock() for _ in range(3)]
        with (
            patch(
                "custom_components.nordpool.sensor.async_dispatcher_connect",
                side_effect=unsubscribes,
            ),
            patch("custom_components.nordpool.sensor.dt_utils.now", return_value=NOW),
        ):
            await sensor.async_added_to_hass()
            self.assertFalse(sensor.available)

            await sensor.handle_new_hr()
            self.assertTrue(sensor.available)
            self.assertEqual(sensor.native_value, 1.0)
            self.assertEqual(sensor.unique_id, "nordpool_kwh_se3_sek_3_10_025")

        sensor.add_to_platform_abort()
        for unsubscribe in unsubscribes:
            unsubscribe.assert_called_once()

    async def test_missing_tomorrow_does_not_poison_today_after_midnight(self):
        api = NordpoolData(Mock())
        api._refresh = AsyncMock(side_effect=[
            price_data(), InvalidValueException("tomorrow missing"), price_data(),
        ])

        await api.today("SE3", "SEK")
        with self.assertRaises(InvalidValueException):
            await api.tomorrow("SE3", "SEK")
        self.assertEqual(len((await api.today("SE3", "SEK"))["values"]), 96)

        await api.rollover()
        self.assertEqual(len((await api.today("SE3", "SEK"))["values"]), 96)
        self.assertEqual(api._refresh.await_count, 3)
