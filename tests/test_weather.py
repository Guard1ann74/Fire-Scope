"""Погода Open-Meteo: показатель Нестерова, ветер, выбор API и кеш (без сети)."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from service.weather import Weather, WeatherError, compass, danger_class


def payload(start: date, days: int, temperature=25.0, dew=15.0, rain_day=None, rain=10.0):
    times, t, td, rh, ws, wd, gust, precip = [], [], [], [], [], [], [], []
    for day in range(days):
        for hour in range(24):
            moment = datetime(start.year, start.month, start.day) + timedelta(days=day, hours=hour)
            times.append(moment.isoformat(timespec="minutes"))
            t.append(temperature)
            td.append(dew)
            rh.append(30)
            ws.append(6.0)
            wd.append(45.0)
            gust.append(11.0)
            precip.append(rain if day == rain_day and hour == 6 else 0.0)
    return {"utc_offset_seconds": 10800, "hourly": {
        "time": times, "temperature_2m": t, "dew_point_2m": td, "relative_humidity_2m": rh,
        "wind_speed_10m": ws, "wind_direction_10m": wd, "wind_gusts_10m": gust, "precipitation": precip}}


class WeatherTests(TestCase):
    def make(self, data, cache=None, today=date(2026, 10, 11)):
        urls = []

        def fetch(url):
            urls.append(url)
            return data

        return Weather(cache, fetch=fetch, today=lambda: today), urls

    def test_nesterov_accumulates_and_rain_resets(self):
        fire = datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc)  # 14:00 местного
        start = fire.date() - timedelta(days=30)
        weather, urls = self.make(payload(start, 31))
        result = weather.at(37.8, 45.26, fire)
        self.assertEqual(result["nesterov"], 31 * 25 * 10)  # 31 сухой день по 25·(25−15)
        self.assertEqual(result["danger_class"], 4)
        self.assertEqual(result["dry_days"], 31)
        self.assertEqual(result["at"], "2026-10-10T14:00")
        self.assertIn("api.open-meteo.com/v1/forecast", urls[0])

        rainy, _ = self.make(payload(start, 31, rain_day=28))
        result = rainy.at(37.8, 45.26, fire)
        self.assertEqual(result["nesterov"], 2 * 250)  # дождь 08.10 обнулил: остались 09 и 10.10
        self.assertEqual(result["danger_class"], 2)
        self.assertEqual(result["precip_24h_mm"], 0.0)

    def test_fire_before_noon_does_not_count_its_day(self):
        fire = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)  # 09:00 местного
        weather, _ = self.make(payload(fire.date() - timedelta(days=30), 31))
        self.assertEqual(weather.at(37.8, 45.26, fire)["nesterov"], 30 * 250)

    def test_wind_direction_and_spread(self):
        fire = datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc)
        weather, _ = self.make(payload(fire.date() - timedelta(days=30), 31))
        result = weather.at(37.8, 45.26, fire)
        self.assertEqual((result["wind_from"], result["spread_to"]), ("СВ", "ЮЗ"))
        self.assertEqual(compass(359), "С")
        self.assertEqual([danger_class(v) for v in (0, 300, 301, 4000, 10001)], [1, 1, 2, 3, 5])

    def test_old_dates_use_era5_and_are_cached_on_disk(self):
        fire = datetime(2025, 8, 10, 11, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            weather, urls = self.make(payload(fire.date() - timedelta(days=30), 31), Path(directory))
            first = weather.at(37.8, 45.26, fire)
            again, more_urls = self.make({"error": True, "reason": "не должен вызываться"}, Path(directory))
            self.assertEqual(again.at(37.8, 45.26, fire), first)
        self.assertIn("archive-api.open-meteo.com", urls[0])
        self.assertEqual(first["source"], "Open-Meteo, реанализ ERA5")
        self.assertEqual(more_urls, [])

    def test_error_is_reported(self):
        weather, _ = self.make({"error": True, "reason": "out of range"})
        with self.assertRaises(WeatherError):
            weather.at(37.8, 45.26, datetime(2026, 10, 10, 11, tzinfo=timezone.utc))
