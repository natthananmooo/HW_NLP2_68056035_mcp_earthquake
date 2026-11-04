"""Seismic & Insurance Analytics MCP Server (Streamable HTTP).

Endpoint : http://localhost:8000/mcp
Health   : http://localhost:8000/health
Logs go to stderr only.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
TMD_URL = "https://data.tmd.go.th/api/DailySeismicEvent/v1/"
TMD_PARAMS = {"uid": "api", "ukey": "api12345", "format": "json"}  # public demo keys
THAILAND_TZ = timezone(timedelta(hours=7))  # TMD days follow Thai time, not container UTC
HTTP_TIMEOUT = 20.0

DEFAULT_PAYOUT_CURVE = [
    {"max_distance_km": 50, "payout_percent": 0.5},
    {"max_distance_km": 100, "payout_percent": 0.25},
    {"max_distance_km": 200, "payout_percent": 0.1},
]

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[seismic-mcp] %(message)s",
)
log = logging.getLogger("seismic-mcp")

mcp = FastMCP("seismic-mcp-server", host=HOST, port=PORT, stateless_http=True, json_response=True)
mcp._mcp_server.version = "0.1.0"  # FastMCP has no public version option


# ---------------------------------------------------------------- input models
class MatchAsset(BaseModel):
    model_config = ConfigDict(extra="allow")
    asset_id: str
    name: str | None = None
    latitude: float
    longitude: float
    sum_insured: float | None = None


class PayoutAsset(MatchAsset):
    sum_insured: float


class PayoutBand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_distance_km: float
    payout_percent: Annotated[float, Field(ge=0, le=1)]


# --------------------------------------------------------------------- helpers
def _number(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def normalize_usgs(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]
    epoch_ms = props.get("time")
    time = (
        datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if epoch_ms is not None
        else None
    )
    return {
        "id": feature.get("id"),
        "time": time,
        "place": props.get("place"),
        "magnitude": props.get("mag"),
        "latitude": coords[1],
        "longitude": coords[0],
        "depth_km": coords[2],
        "source": "USGS",
        "url": props.get("url"),
        "raw": feature,
    }


def _tmd_time(item: dict[str, Any]) -> str | None:
    if item.get("DateTime"):
        return item["DateTime"]
    thai_time = item.get("DateTimeThai")
    return f'{thai_time.split(".")[0].replace(" ", "T")}+07:00' if thai_time else None


def normalize_tmd(item: dict[str, Any]) -> dict[str, Any]:
    """Accept both the spec shape and the live shape, which has no EventID."""
    latitude = _number(item.get("Latitude"))
    longitude = _number(item.get("Longitude"))
    event_id = item.get("EventID")
    if event_id is None:
        stamp = str(_first(item, "DateTimeUTC", "DateTimeThai") or "").split(".")[0]
        event_id = f'{"".join(c for c in stamp if c.isdigit())}-{latitude}-{longitude}'
    return {
        "id": f"TMD-{event_id}",
        "time": _tmd_time(item),
        "place": _first(item, "Location", "OriginThai", "TitleThai"),
        "magnitude": _number(item.get("Magnitude")),
        "latitude": latitude,
        "longitude": longitude,
        "depth_km": _number(_first(item, "DepthKm", "Depth")),
        "source": "TMD_DailySeismicEvent",
        "url": _first(item, "URL", "Url", "Link"),
        "raw": item,
    }


def _tmd_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("DailySeismicEvent")
    if items is None:
        items = payload.get("DailyEarthquakes")  # key used by the live API
    if isinstance(items, dict):
        return [items]
    return [i for i in items or [] if isinstance(i, dict)]


async def _get_json(url: str, params: dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"Upstream returned HTTP {exc.response.status_code}: {url}") from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"Could not reach upstream {url}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"Upstream returned invalid JSON: {url}") from exc


async def _fetch_tmd(date: str) -> list[dict[str, Any]]:
    payload = await _get_json(TMD_URL, {**TMD_PARAMS, "Date": date})
    events = [normalize_tmd(item) for item in _tmd_items(payload)]
    # The live API ignores Date and returns the year-to-date feed, so filter here.
    return [e for e in events if str(e["time"] or "")[:10] == date]


def _today() -> datetime:
    return datetime.now(THAILAND_TZ)


# ------------------------------------------------------------------ data tools
@mcp.tool()
async def usgs_search_earthquakes(
    starttime: Annotated[str, Field(description="YYYY-MM-DD or ISO8601.")],
    endtime: Annotated[str, Field(description="YYYY-MM-DD or ISO8601.")],
    minmagnitude: Annotated[float, Field(description="Minimum magnitude.")] = 4.5,
    maxmagnitude: Annotated[float | None, Field(description="Maximum magnitude, optional.")] = None,
    latitude: Annotated[float | None, Field(description="Center latitude for radius search, optional.")] = None,
    longitude: Annotated[float | None, Field(description="Center longitude for radius search, optional.")] = None,
    maxradiuskm: Annotated[float | None, Field(description="Search radius in kilometers, optional.")] = None,
    limit: Annotated[int, Field(description="Maximum number of events.", ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """Search earthquakes from the USGS FDSN API with basic filters."""
    radius = [latitude, longitude, maxradiuskm]
    if any(v is not None for v in radius) and any(v is None for v in radius):
        raise ValueError("latitude, longitude and maxradiuskm must be provided together")
    query = {
        "starttime": starttime,
        "endtime": endtime,
        "minmagnitude": minmagnitude,
        "maxmagnitude": maxmagnitude,
        "latitude": latitude,
        "longitude": longitude,
        "maxradiuskm": maxradiuskm,
        "limit": limit,
    }
    query = {k: v for k, v in query.items() if v is not None}
    payload = await _get_json(USGS_URL, {"format": "geojson", **query})
    return {"events": [normalize_usgs(f) for f in payload.get("features", [])], "query": query}


@mcp.tool()
async def tmd_get_daily_earthquakes(
    date: Annotated[str | None, Field(description="YYYY-MM-DD. If omitted, use today's date.")] = None,
) -> dict[str, Any]:
    """Get daily earthquakes from the TMD DailySeismicEvent demo API."""
    resolved = date or _today().strftime("%Y-%m-%d")
    datetime.strptime(resolved, "%Y-%m-%d")  # rejects a malformed date
    return {"events": await _fetch_tmd(resolved), "query": {"date": resolved}}


@mcp.tool()
async def tmd_get_recent_earthquakes(
    days: Annotated[int, Field(description="Number of days back from today (including today).", ge=1, le=7)] = 3,
) -> dict[str, Any]:
    """Get recent earthquakes from TMD over the last N days using DailySeismicEvent."""
    today = _today()
    events: dict[str, dict[str, Any]] = {}
    for offset in range(days):
        date = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        for event in await _fetch_tmd(date):
            events.setdefault(event["id"], event)
    ordered = sorted(events.values(), key=lambda e: str(e["time"] or ""), reverse=True)
    return {"events": ordered, "query": {"days": days}}


# ------------------------------------------------------------- analytics tools
@mcp.tool()
def match_events_to_assets(
    events: Annotated[list[dict[str, Any]], Field(description="List of EarthquakeEvent objects.")],
    assets: Annotated[list[MatchAsset], Field(description="Insured assets with coordinates.")],
    max_distance_km: Annotated[float, Field(description="Max distance to consider an asset affected.")] = 100,
    min_magnitude: Annotated[float, Field(description="Minimum magnitude to consider.")] = 4.5,
) -> dict[str, Any]:
    """Match earthquake events to insured assets within a given radius and magnitude threshold."""
    usable = []
    for event in events:
        magnitude, lat, lon = (_number(event.get(k)) for k in ("magnitude", "latitude", "longitude"))
        if magnitude is not None and lat is not None and lon is not None and magnitude >= min_magnitude:
            usable.append((event, lat, lon))

    matches = []
    for asset in assets:
        hits = []
        for event, lat, lon in usable:
            distance = haversine_km(lat, lon, asset.latitude, asset.longitude)
            if distance <= max_distance_km:
                hits.append({"event": event, "distance_km": round(distance, 2)})
        if hits:
            matches.append(
                {
                    "asset_id": asset.asset_id,
                    "name": asset.name,
                    "sum_insured": asset.sum_insured,
                    "events": sorted(hits, key=lambda h: h["distance_km"]),
                }
            )
    return {
        "matches": matches,
        "parameters": {"max_distance_km": max_distance_km, "min_magnitude": min_magnitude},
    }


@mcp.tool()
def compute_parametric_payout(
    event: Annotated[dict[str, Any], Field(description="Single EarthquakeEvent to evaluate.")],
    assets: Annotated[list[PayoutAsset], Field(description="Assets with coordinates and sum_insured.")],
    min_magnitude_trigger: Annotated[float, Field(description="Minimum magnitude to trigger any payout.")] = 5.0,
    max_distance_km_trigger: Annotated[float, Field(description="Max distance for eligibility.")] = 200,
    payout_curve: Annotated[
        list[PayoutBand] | None, Field(description="Optional distance-based payout curve.")
    ] = None,
) -> dict[str, Any]:
    """Compute a simple parametric payout for an earthquake event and a set of assets, based on distance and magnitude thresholds."""
    magnitude, lat, lon = (_number(event.get(k)) for k in ("magnitude", "latitude", "longitude"))
    if magnitude is None or lat is None or lon is None:
        raise ValueError("event must include numeric magnitude, latitude and longitude")

    curve = (
        [band.model_dump() for band in payout_curve]
        if payout_curve is not None
        else [dict(band) for band in DEFAULT_PAYOUT_CURVE]
    )
    triggered = magnitude >= min_magnitude_trigger
    payouts = []
    for asset in assets:
        distance = haversine_km(lat, lon, asset.latitude, asset.longitude)
        percent = 0.0
        if triggered and distance <= max_distance_km_trigger:
            band = next((b for b in curve if distance <= b["max_distance_km"]), None)
            percent = band["payout_percent"] if band else 0.0
        payouts.append(
            {
                "asset_id": asset.asset_id,
                "name": asset.name,
                "distance_km": round(distance, 2),
                "sum_insured": asset.sum_insured,
                "payout_percent": percent,
                "payout_amount": round(asset.sum_insured * percent, 2),
            }
        )
    return {
        "event_id": event.get("id"),
        "event_magnitude": magnitude,
        "parameters": {
            "min_magnitude_trigger": min_magnitude_trigger,
            "max_distance_km_trigger": max_distance_km_trigger,
            "payout_curve": curve,
        },
        "asset_payouts": payouts,
        "total_payout": round(sum(p["payout_amount"] for p in payouts), 2),
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "seismic-mcp-server", "version": "0.1.0"})


if __name__ == "__main__":
    log.info("Starting on %s:%s", HOST, PORT)
    log.info("  MCP    : POST http://localhost:%s/mcp", PORT)
    log.info("  Health : GET  http://localhost:%s/health", PORT)
    mcp.run(transport="streamable-http")
