"""Current weather outside the shed, for colour: "a wet night out there, bone dry in here". From
Open-Meteo (free, no API key). Fetched in the background and cached, so no line ever waits on it;
when it's unavailable or stale the booth simply doesn't get a weather fact."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
STALE_SECONDS = 2 * 3600

# WMO weather interpretation codes (Open-Meteo's weather_code).
_WMO = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "heavy rain showers", 82: "violent rain showers", 85: "snow showers",
    86: "heavy snow showers", 95: "a thunderstorm", 96: "a thunderstorm with hail", 99: "a thunderstorm with heavy hail",
}


@dataclass
class Conditions:
    fetched_at: float
    temp_f: float
    feels_f: float | None
    sky: str
    wind_mph: float | None
    gust_mph: float | None
    is_day: bool
    sunset: datetime | None


def parse(body: dict, now: float | None = None) -> Conditions:
    cur = body["current"]
    sunset = None
    try:
        sunset = datetime.fromisoformat(body["daily"]["sunset"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return Conditions(
        fetched_at=now if now is not None else time.time(), temp_f=float(cur["temperature_2m"]),
        feels_f=cur.get("apparent_temperature"), sky=_WMO.get(int(cur.get("weather_code") or 0), "mixed weather"),
        wind_mph=cur.get("wind_speed_10m"), gust_mph=cur.get("wind_gusts_10m"), is_day=bool(cur.get("is_day", 1)),
        sunset=sunset,
    )


def describe(c: Conditions, place: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    parts = [f"{c.temp_f:.0f}°F"]
    if c.feels_f is not None and abs(c.feels_f - c.temp_f) >= 5:
        parts[0] += f" (feels like {c.feels_f:.0f})"
    parts.append(c.sky)
    if c.wind_mph is not None and c.wind_mph >= 8:
        parts.append(f"wind {c.wind_mph:.0f} mph" + (f" gusting {c.gust_mph:.0f}" if c.gust_mph and c.gust_mph >= c.wind_mph + 8 else ""))
    elif c.wind_mph is not None:
        parts.append("barely any wind")
    if not c.is_day:
        parts.append("dark out")
    elif c.sunset and now < c.sunset and (c.sunset - now).total_seconds() <= 2 * 3600:
        parts.append(f"sunset at {c.sunset.strftime('%I:%M %p').lstrip('0').lower()}")
    where = f" in {place}" if place else ""
    return (f"Outside the shed{where} right now: {', '.join(parts)}. Inside the shed it's always dry and calm, "
            "so the weather doesn't touch play: it's colour (what they're escaping, or missing), for the odd "
            "line, not every one.")


class Weather:
    def __init__(self, latitude: float, longitude: float, place: str = "", timeout: float = 6.0) -> None:
        self.latitude, self.longitude, self.place, self.timeout = latitude, longitude, place, timeout
        self.current: Conditions | None = None

    async def refresh(self) -> Conditions | None:
        params = {
            "latitude": self.latitude, "longitude": self.longitude, "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m,is_day",
            "daily": "sunset", "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        }
        try:   # never takes the booth off air: a failed fetch keeps the last good reading until it's stale
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(URL, params=params)
                resp.raise_for_status()
                self.current = parse(resp.json())
            log.info("Weather: %s", describe(self.current, self.place).split(". Inside")[0])
        except Exception as e:
            log.warning("Weather fetch failed: %r", e)
        return self.current

    def facts(self, now: float | None = None) -> list[str]:
        c = self.current
        if c is None or (now if now is not None else time.time()) - c.fetched_at > STALE_SECONDS:
            return []
        return [describe(c, self.place)]
