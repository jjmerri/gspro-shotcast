from datetime import datetime

from commentator.weather import Weather, describe, parse

BODY = {"current": {"temperature_2m": 44.2, "apparent_temperature": 37.0, "weather_code": 63, "wind_speed_10m": 14.0,
                    "wind_gusts_10m": 27.0, "is_day": 1},
        "daily": {"sunset": ["2026-10-20T17:53"]}}


def test_weather_reads_as_colour_outside_the_shed():
    c = parse(BODY, now=1000.0)
    text = describe(c, "Nottingham", now=datetime(2026, 10, 20, 17, 0))
    assert text.startswith("Outside the shed in Nottingham right now: 44°F (feels like 37), rain, "
                           "wind 14 mph gusting 27, sunset at 5:53 pm.")
    assert "Inside the shed it's always dry" in text
    calm_night = parse({"current": {"temperature_2m": 61, "weather_code": 0, "wind_speed_10m": 3, "is_day": 0}}, 0)
    assert describe(calm_night, "") == describe(calm_night, "", datetime(2026, 1, 1))
    assert "61°F, clear skies, barely any wind, dark out." in describe(calm_night, "")


def test_weather_facts_go_quiet_when_stale_or_never_fetched():
    w = Weather(43.11, -71.10, "Nottingham")
    assert w.facts() == []
    w.current = parse(BODY, now=1000.0)
    assert w.facts(now=1000.0 + 60) and w.facts(now=1000.0 + 3 * 3600) == []


def test_engine_gives_the_opener_the_weather_but_not_every_segment(tmp_path):
    from tests.test_round import _engine

    eng = _engine(tmp_path)
    assert eng.weather is None and eng._weather_facts() == []     # off by default
    eng.weather = Weather(43.11, -71.10, "Nottingham")
    import time
    eng.weather.current = parse(BODY, now=time.time())
    assert eng._weather_facts()[0].startswith("Outside the shed in Nottingham")
    assert sum(bool(eng._weather_facts(0.2)) for _ in range(200)) < 80
