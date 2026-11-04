import asyncio
import pytest
import seismic_mcp_server as s

EVENT = {"id": "E1", "magnitude": 6.0, "latitude": 13.75, "longitude": 100.5}
ASSETS = [
    {"asset_id": "A", "name": "near", "latitude": 13.8, "longitude": 100.5, "sum_insured": 1000000},
    {"asset_id": "B", "name": "mid", "latitude": 14.5, "longitude": 100.5, "sum_insured": 1000000},
    {"asset_id": "C", "name": "far", "latitude": 18.8, "longitude": 98.98, "sum_insured": 1000000},
]
assets = [s.PayoutAsset(**a) for a in ASSETS]


def test_haversine_one_degree_latitude():
    assert s.haversine_km(0, 0, 1, 0) == pytest.approx(111.19, abs=0.05)


def test_default_curve_and_total():
    r = s.compute_parametric_payout(EVENT, assets)
    assert [p["payout_percent"] for p in r["asset_payouts"]] == [0.5, 0.25, 0.0]
    assert r["total_payout"] == 750000


def test_below_trigger_pays_nothing():
    r = s.compute_parametric_payout({**EVENT, "magnitude": 4.9}, assets)
    assert r["total_payout"] == 0


def test_magnitude_exactly_at_trigger_pays():
    r = s.compute_parametric_payout({**EVENT, "magnitude": 5.0}, assets)
    assert r["total_payout"] > 0


def test_match_filters_magnitude_and_distance():
    small = {**EVENT, "id": "E2", "magnitude": 3}
    no_mag = {**EVENT, "id": "E3", "magnitude": None}
    r = s.match_events_to_assets([EVENT, small, no_mag], [s.MatchAsset(**a) for a in ASSETS])
    assert [m["asset_id"] for m in r["matches"]] == ["A", "B"]
    assert [h["event"]["id"] for h in r["matches"][0]["events"]] == ["E1"]


def test_normalize_usgs():
    e = s.normalize_usgs({
        "id": "us1",
        "properties": {"time": 0, "place": "X", "mag": 5.1, "url": "u"},
        "geometry": {"coordinates": [100.0, 13.0, 10.0]},
    })
    assert (e["latitude"], e["longitude"], e["depth_km"]) == (13.0, 100.0, 10.0)
    assert e["time"] == "1970-01-01T00:00:00Z"


def test_tmd_recent_dedupes_and_sorts(monkeypatch):
    item_a = {"EventID": "1", "DateTime": "2026-03-06T09:00:00+07:00", "Magnitude": "4.6",
              "DepthKm": 10, "Latitude": 18.1, "Longitude": 98.4, "Location": "A"}
    item_b = {**item_a, "EventID": "2", "DateTime": "2026-03-07T09:00:00+07:00"}

    async def fake(url, params):
        return {"Header": {}, "DailySeismicEvent": [item_a, item_b]}

    monkeypatch.setattr(s, "_get_json", fake)
    monkeypatch.setattr(s, "_today", lambda: s.datetime(2026, 3, 7, tzinfo=s.THAILAND_TZ))
    r = asyncio.run(s.tmd_get_recent_earthquakes(days=3))
    assert [e["id"] for e in r["events"]] == ["TMD-2", "TMD-1"]
    assert r["events"][0]["magnitude"] == 4.6


def test_tmd_empty_day(monkeypatch):
    async def fake(url, params):
        return {"Header": {}, "DailySeismicEvent": None}

    monkeypatch.setattr(s, "_get_json", fake)
    assert asyncio.run(s.tmd_get_daily_earthquakes("2026-03-07"))["events"] == []


def test_partial_radius_rejected():
    with pytest.raises(ValueError):
        asyncio.run(s.usgs_search_earthquakes("2026-03-01", "2026-03-07", latitude=13.0))


def test_tmd_live_shape_is_filtered_to_requested_date(monkeypatch):
    live = {"DateTimeUTC": "2026-11-04 07:52:00.000", "DateTimeThai": "2026-11-04 14:52:00.000",
            "OriginThai": "Phayao", "Depth": "2", "Magnitude": "2.3",
            "Latitude": "19.3", "Longitude": "100.3"}
    other = {**live, "DateTimeUTC": "2026-11-03 01:00:00.000", "DateTimeThai": "2026-11-03 08:00:00.000"}

    async def fake(url, params):
        return {"header": {}, "DailyEarthquakes": [live, other]}

    monkeypatch.setattr(s, "_get_json", fake)
    r = asyncio.run(s.tmd_get_daily_earthquakes("2026-11-04"))
    assert len(r["events"]) == 1
    e = r["events"][0]
    assert e["time"] == "2026-11-04T14:52:00+07:00"
    assert (e["place"], e["magnitude"], e["depth_km"]) == ("Phayao", 2.3, 2.0)
    assert e["id"] == "TMD-20261104075200-19.3-100.3"
