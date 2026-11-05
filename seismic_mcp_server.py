"""Seismic & Insurance Analytics MCP Server (Streamable HTTP).

Endpoint : http://localhost:8000/mcp
Health   : http://localhost:8000/health
Logs go to stderr only.
"""
# ส่วนที่ 1: นำเข้าไลบรารี
# - httpx: เรียก API ภายนอก
# - FastMCP: เปลี่ยนฟังก์ชัน Python เป็น MCP Tool
# - pydantic: ตรวจรูปแบบข้อมูลที่รับเข้ามา
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

# ส่วนที่ 2: ค่าคงที่ของระบบ
# URL ของแหล่งข้อมูล และคีย์ทดลองที่ TMD เปิดให้ใช้สาธารณะ
USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
TMD_URL = "https://data.tmd.go.th/api/DailySeismicEvent/v1/"
TMD_PARAMS = {"uid": "api", "ukey": "api12345", "format": "json"}  # public demo keys
THAILAND_TZ = timezone(timedelta(hours=7))  # TMD days follow Thai time, not container UTC
# ถ้า API ไม่ตอบภายใน 20 วินาที จะถือว่าเรียกไม่สำเร็จ
HTTP_TIMEOUT = 20.0

# ตารางจ่ายเงินเริ่มต้น: ยิ่งใกล้จุดศูนย์กลาง ยิ่งจ่ายมาก
# payout_percent เป็นสัดส่วนของทุนประกัน เช่น 0.5 คือ 50%
DEFAULT_PAYOUT_CURVE = [
    {"max_distance_km": 50, "payout_percent": 0.5},
    {"max_distance_km": 100, "payout_percent": 0.25},
    {"max_distance_km": 200, "payout_percent": 0.1},
]

# 0.0.0.0 ทำให้เข้าถึงเซิร์ฟเวอร์จากนอก Docker container ได้
# เปลี่ยนค่าได้ด้วย environment variable ชื่อ HOST และ PORT
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ส่ง log ไปที่ stderr เพื่อไม่ให้ปนกับคำตอบของ MCP
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[seismic-mcp] %(message)s",
)
log = logging.getLogger("seismic-mcp")

# สร้าง MCP Server ที่ใช้เก็บ Tool ทั้งหมด
# stateless_http=True: แต่ละคำขอทำงานแยกกัน ไม่ต้องจำ session
# json_response=True: ตอบกลับเป็น JSON
mcp = FastMCP("seismic-mcp-server", host=HOST, port=PORT, stateless_http=True, json_response=True)
mcp._mcp_server.version = "0.1.0"  # FastMCP has no public version option


# ---------------------------------------------------------------- input models
# โครงสร้างทรัพย์สินสำหรับการจับคู่ ต้องมีรหัสและพิกัด ส่วนทุนประกันไม่บังคับ
# extra="allow" ยอมรับฟิลด์เพิ่มเติมจากที่กำหนด
class MatchAsset(BaseModel):
    model_config = ConfigDict(extra="allow")
    asset_id: str
    name: str | None = None
    latitude: float
    longitude: float
    sum_insured: float | None = None


# โครงสร้างทรัพย์สินสำหรับคำนวณเงิน จึงบังคับให้มี sum_insured
class PayoutAsset(MatchAsset):
    sum_insured: float


# หนึ่งช่วงของตารางจ่ายเงิน
# extra="forbid" ไม่รับฟิลด์อื่น และ payout_percent ต้องอยู่ระหว่าง 0 ถึง 1
class PayoutBand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_distance_km: float
    # บอกช่วงค่าใน schema และให้ Tool ตรวจเอง เพื่อตอบ error พร้อม hint
    payout_percent: Annotated[float, Field(json_schema_extra={"minimum": 0, "maximum": 1})]


# ------------------------------------------------------------- error handling
# LVL 1 Recoverable: คืน dict ที่มี error, code, hint เพื่อให้ LLM แก้ argument หรือลองใหม่ได้
# LVL 2 Fatal: บันทึก log แล้ว raise เพราะเป็นปัญหาที่ LLM แก้เองไม่ได้
def error_result(error: str, code: str, hint: str) -> dict[str, str]:
    return {"error": error, "code": code, "hint": hint}


class UpstreamError(Exception):
    """Recoverable failure while calling an external earthquake API."""

    def __init__(self, error: str, code: str, hint: str) -> None:
        super().__init__(error)
        self.result = error_result(error, code, hint)


# --------------------------------------------------------------------- helpers
# แปลงค่าเป็นตัวเลข เพราะ API อาจส่งตัวเลขมาเป็นข้อความ เช่น "4.6"
# ถ้าแปลงไม่ได้จะคืน None แทนการทำให้โปรแกรมหยุด
def _number(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


# คืนค่าจากคีย์แรกที่มีข้อมูล ใช้เมื่อ API ตั้งชื่อฟิลด์ได้หลายแบบ
def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


# คำนวณระยะทางบนผิวโลกจากพิกัด 2 จุดด้วยสูตร Haversine (หน่วยกิโลเมตร)
# 6371.0088 คือรัศมีเฉลี่ยของโลก
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


# แปลงข้อมูล USGS ให้เป็นรูปแบบ EarthquakeEvent เดียวกับทุกแหล่งข้อมูล
def normalize_usgs(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]
    # USGS ส่งเวลาเป็นมิลลิวินาที จึงแปลงเป็น ISO8601 แบบ UTC
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
        # GeoJSON เรียงพิกัดเป็น [longitude, latitude, depth]
        "latitude": coords[1],
        "longitude": coords[0],
        "depth_km": coords[2],
        "source": "USGS",
        "url": props.get("url"),
        "raw": feature,
    }


# จัดเวลาของ TMD ให้เป็น ISO8601 และระบุเขตเวลาไทย +07:00
def _tmd_time(item: dict[str, Any]) -> str | None:
    if item.get("DateTime"):
        return item["DateTime"]
    thai_time = item.get("DateTimeThai")
    return f'{thai_time.split(".")[0].replace(" ", "T")}+07:00' if thai_time else None


def normalize_tmd(item: dict[str, Any]) -> dict[str, Any]:
    """Accept both the spec shape and the live shape, which has no EventID."""
    # API จริงอาจส่งละติจูดและลองจิจูดเป็นข้อความ จึงแปลงเป็นตัวเลขก่อน
    latitude = _number(item.get("Latitude"))
    longitude = _number(item.get("Longitude"))
    event_id = item.get("EventID")
    # API จริงไม่มี EventID จึงสร้างรหัสจากเวลาและพิกัด เพื่อใช้ตัดข้อมูลซ้ำ
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


# ดึงรายการแผ่นดินไหวจากคำตอบของ TMD
# รองรับทั้งคีย์ใน spec และคีย์ DailyEarthquakes จาก API จริง
def _tmd_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("DailySeismicEvent")
    if items is None:
        items = payload.get("DailyEarthquakes")  # key used by the live API
    if isinstance(items, dict):
        return [items]
    return [i for i in items or [] if isinstance(i, dict)]


# ฟังก์ชันกลางสำหรับเรียก API ภายนอก
# แปลงปัญหาของ API เป็น UpstreamError ที่มี code และ hint แล้วส่งต่อให้ Tool
async def _get_json(url: str, params: dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params)
            # ถ้า API ตอบ 4xx หรือ 5xx จะเกิด HTTPStatusError
            response.raise_for_status()
            return response.json()
    # หมดเวลา: ลองใหม่ได้ จึงเป็น recoverable
    except httpx.TimeoutException as exc:
        raise UpstreamError(
            f"Request to {url} timed out",
            "UPSTREAM_TIMEOUT",
            "Retry later or request a smaller time range.",
        ) from exc
    # API ตอบกลับ แต่สถานะเป็น error
    except httpx.HTTPStatusError as exc:
        raise UpstreamError(
            f"Upstream returned HTTP {exc.response.status_code}: {url}",
            "UPSTREAM_HTTP_ERROR",
            "Check the arguments. If they are valid, retry later.",
        ) from exc
    # เชื่อมต่อไม่ได้
    except httpx.HTTPError as exc:
        raise UpstreamError(
            f"Could not reach upstream {url}: {exc}",
            "UPSTREAM_UNAVAILABLE",
            "Retry later. If it continues, check the network connection.",
        ) from exc
    # API ตอบกลับ แต่ข้อมูลไม่ใช่ JSON
    except ValueError as exc:
        raise UpstreamError(
            f"Upstream returned invalid JSON: {url}",
            "UPSTREAM_INVALID_RESPONSE",
            "Retry later. Do not guess missing earthquake data.",
        ) from exc


async def _fetch_tmd(date: str) -> list[dict[str, Any]]:
    payload = await _get_json(TMD_URL, {**TMD_PARAMS, "Date": date})
    events = [normalize_tmd(item) for item in _tmd_items(payload)]
    # The live API ignores Date and returns the year-to-date feed, so filter here.
    return [e for e in events if str(e["time"] or "")[:10] == date]


# คืนวันเวลาปัจจุบันตามเวลาไทย แม้ container จะใช้เวลา UTC
def _today() -> datetime:
    return datetime.now(THAILAND_TZ)


# ส่วนที่ 3: Tool สำหรับดึงข้อมูล
# @mcp.tool() ลงทะเบียนฟังก์ชันด้านล่างให้เป็น MCP Tool
# FastMCP สร้าง schema จากชื่อฟังก์ชัน type hint, Field และ docstring
# json_schema_extra บอกช่วงค่าให้ LLM เห็น และให้ Tool ตรวจเองเพื่อตอบ hint ได้
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
    limit: Annotated[int, Field(description="Maximum number of events.", json_schema_extra={"minimum": 1, "maximum": 200})] = 20,
) -> dict[str, Any]:
    """Search global earthquakes from the USGS FDSN API.

    Use when you need earthquakes by date range, magnitude or distance from a location.

    Do NOT use for Thai Meteorological Department reports. Use `tmd_get_daily_earthquakes`
    or `tmd_get_recent_earthquakes` instead.

    Args:
        starttime: Start time as YYYY-MM-DD or ISO8601.
        endtime: End time as YYYY-MM-DD or ISO8601.
        minmagnitude: Minimum magnitude. Must not exceed maxmagnitude.
        maxmagnitude: Maximum magnitude.
        latitude: Search center latitude in degrees (-90 to 90).
        longitude: Search center longitude in degrees (-180 to 180).
        maxradiuskm: Search radius in km (>0).
        limit: Maximum number of events (1-200).

    Latitude, longitude and maxradiuskm must be provided together.

    Returns:
        events (list[EarthquakeEvent]): Matching earthquakes.
        query (dict): Parameters sent to USGS.
        On a recoverable failure: error (str), code (str), hint (str).
    """
    # ตรวจล่วงหน้า: การค้นหาตามรัศมีต้องส่งครบทั้ง 3 ค่า
    radius = [latitude, longitude, maxradiuskm]
    if any(v is not None for v in radius) and any(v is None for v in radius):
        return error_result(
            "latitude, longitude and maxradiuskm must be provided together",
            "INVALID_ARG",
            "Provide all three values or omit all three.",
        )
    if not 1 <= limit <= 200:
        return error_result(f"limit {limit} out of range", "INVALID_ARG", "Use a value between 1 and 200.")
    if maxmagnitude is not None and minmagnitude > maxmagnitude:
        return error_result(
            f"minmagnitude {minmagnitude} exceeds maxmagnitude {maxmagnitude}",
            "INVALID_ARG",
            "Use a minmagnitude that is not greater than maxmagnitude.",
        )
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
    # ตัดพารามิเตอร์ที่ไม่ได้ระบุออกก่อนส่งให้ USGS
    query = {k: v for k, v in query.items() if v is not None}
    try:
        payload = await _get_json(USGS_URL, {"format": "geojson", **query})
        return {"events": [normalize_usgs(f) for f in payload.get("features", [])], "query": query}
    except UpstreamError as exc:
        return exc.result
    except Exception:
        log.exception("unexpected failure in usgs_search_earthquakes")
        raise


@mcp.tool()
async def tmd_get_daily_earthquakes(
    date: Annotated[str | None, Field(description="YYYY-MM-DD. If omitted, use today's date.")] = None,
) -> dict[str, Any]:
    """Get earthquakes reported by TMD for one Thai calendar date.

    Use when you need Thai Meteorological Department data for one specific date.

    Do NOT use for a multi-day period. Use `tmd_get_recent_earthquakes` instead.
    Do NOT use for global searches. Use `usgs_search_earthquakes` instead.

    Args:
        date: Date as YYYY-MM-DD. If omitted, today's date in Thailand is used.

    Returns:
        events (list[EarthquakeEvent]): Earthquakes on that date.
        query (dict): The resolved date.
        On a recoverable failure: error (str), code (str), hint (str).
    """
    # ถ้าไม่ได้ระบุวันที่ ให้ใช้วันนี้ตามเวลาไทย
    resolved = date or _today().strftime("%Y-%m-%d")
    try:
        datetime.strptime(resolved, "%Y-%m-%d")
    except ValueError:
        return error_result(f"date {resolved} is not valid", "INVALID_ARG", "Use the format YYYY-MM-DD.")
    try:
        return {"events": await _fetch_tmd(resolved), "query": {"date": resolved}}
    except UpstreamError as exc:
        return exc.result
    except Exception:
        log.exception("unexpected failure in tmd_get_daily_earthquakes")
        raise


@mcp.tool()
async def tmd_get_recent_earthquakes(
    days: Annotated[int, Field(description="Number of days back from today (including today).", json_schema_extra={"minimum": 1, "maximum": 7})] = 3,
) -> dict[str, Any]:
    """Get recent earthquakes reported by TMD for the last N days.

    Use when you need the latest Thai Meteorological Department reports across several days.

    Do NOT use for one specific date. Use `tmd_get_daily_earthquakes` instead.
    Do NOT use for global searches. Use `usgs_search_earthquakes` instead.

    Args:
        days: Number of days back from today, including today (1-7).

    Returns:
        events (list[EarthquakeEvent]): Unique earthquakes, newest first.
        query (dict): The requested number of days.
        On a recoverable failure: error (str), code (str), hint (str).
    """
    if not 1 <= days <= 7:
        return error_result(f"days {days} out of range", "INVALID_ARG", "Use a value between 1 and 7.")
    try:
        today = _today()
        events: dict[str, dict[str, Any]] = {}
        for offset in range(days):
            # ไล่วันจากวันนี้ย้อนกลับไป
            date = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            for event in await _fetch_tmd(date):
                # เก็บเหตุการณ์ด้วย id เพื่อตัดข้อมูลซ้ำ
                events.setdefault(event["id"], event)
        # เรียงจากเหตุการณ์ล่าสุด
        ordered = sorted(events.values(), key=lambda e: str(e["time"] or ""), reverse=True)
        return {"events": ordered, "query": {"days": days}}
    except UpstreamError as exc:
        return exc.result
    except Exception:
        log.exception("unexpected failure in tmd_get_recent_earthquakes")
        raise


# ส่วนที่ 4: Tool สำหรับวิเคราะห์ประกันภัย
# Tool กลุ่มนี้คำนวณจากข้อมูลที่ส่งเข้ามา ไม่ได้เรียก API ภายนอก
# ------------------------------------------------------------- analytics tools
@mcp.tool()
def match_events_to_assets(
    events: Annotated[list[dict[str, Any]], Field(description="List of EarthquakeEvent objects.")],
    assets: Annotated[list[MatchAsset], Field(description="Insured assets with coordinates.")],
    max_distance_km: Annotated[float, Field(description="Max distance to consider an asset affected.")] = 100,
    min_magnitude: Annotated[float, Field(description="Minimum magnitude to consider.")] = 4.5,
) -> dict[str, Any]:
    """Find insured assets near earthquake events.

    Use when you have EarthquakeEvent objects and need to know which assets are affected.

    Do NOT use to fetch earthquakes. Use `usgs_search_earthquakes` or the TMD tools instead.
    Do NOT use to calculate money. Use `compute_parametric_payout` instead.

    Args:
        events: EarthquakeEvent objects with magnitude, latitude and longitude.
        assets: Assets with asset_id, latitude and longitude. name and sum_insured are optional.
        max_distance_km: Maximum epicenter-to-asset distance in km (>=0).
        min_magnitude: Minimum magnitude to consider.

    Events with missing magnitude or coordinates are skipped.

    Returns:
        matches (list): asset_id, name, sum_insured and matched events with distance_km.
        parameters (dict): max_distance_km and min_magnitude.
        On a recoverable failure: error (str), code (str), hint (str).
    """
    if max_distance_km < 0:
        return error_result(
            f"max_distance_km {max_distance_km} out of range",
            "INVALID_ARG",
            "Use a value of 0 or greater.",
        )
    try:
        # ขั้นที่ 1: เก็บเฉพาะเหตุการณ์ที่มีข้อมูลครบและถึงเกณฑ์ขนาด
        usable = []
        for event in events:
            magnitude, lat, lon = (_number(event.get(k)) for k in ("magnitude", "latitude", "longitude"))
            if magnitude is not None and lat is not None and lon is not None and magnitude >= min_magnitude:
                usable.append((event, lat, lon))

        # ขั้นที่ 2: คำนวณระยะจากทรัพย์สินแต่ละรายการไปยังแต่ละเหตุการณ์
        matches = []
        for asset in assets:
            hits = []
            for event, lat, lon in usable:
                distance = haversine_km(lat, lon, asset.latitude, asset.longitude)
                if distance <= max_distance_km:
                    hits.append({"event": event, "distance_km": round(distance, 2)})
            # ส่งกลับเฉพาะทรัพย์สินที่มีเหตุการณ์อยู่ในรัศมี เรียงจากใกล้ไปไกล
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
    except Exception:
        log.exception("unexpected failure in match_events_to_assets")
        raise


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
    """Calculate parametric payouts for one earthquake.

    Use when an EarthquakeEvent is available and you need payout amounts for insured assets.

    Do NOT use to fetch earthquakes. Use `usgs_search_earthquakes` or the TMD tools instead.
    To list nearby assets, use `match_events_to_assets`.

    Args:
        event: EarthquakeEvent with numeric magnitude, latitude and longitude.
        assets: Assets with asset_id, latitude, longitude and sum_insured in THB (>=0).
        min_magnitude_trigger: Minimum magnitude that can trigger a payout.
        max_distance_km_trigger: Maximum epicenter-to-asset distance in km (>=0).
        payout_curve: Distance bands. payout_percent is a fraction from 0.0 to 1.0.

    Returns:
        event_id (str): Evaluated event.
        event_magnitude (float): Event magnitude.
        parameters (dict): Triggers and payout curve used.
        asset_payouts (list): asset_id, name, distance_km, sum_insured, payout_percent, payout_amount.
        total_payout (float): Total payout in THB.
        On a recoverable failure: error (str), code (str), hint (str).
    """
    magnitude, lat, lon = (_number(event.get(k)) for k in ("magnitude", "latitude", "longitude"))
    # ตรวจล่วงหน้า: ต้องมีขนาดและพิกัดเป็นตัวเลขก่อนคำนวณ
    if magnitude is None or lat is None or lon is None:
        return error_result(
            "event must include numeric magnitude, latitude and longitude",
            "INVALID_ARG",
            "Pass an EarthquakeEvent returned by an earthquake data tool.",
        )
    if max_distance_km_trigger < 0:
        return error_result(
            f"max_distance_km_trigger {max_distance_km_trigger} out of range",
            "INVALID_ARG",
            "Use a value of 0 or greater.",
        )
    for asset in assets:
        if asset.sum_insured < 0:
            return error_result(
                f"sum_insured of asset {asset.asset_id} is negative",
                "INVALID_ARG",
                "Use a sum_insured of 0 or greater.",
            )
    for band in payout_curve or []:
        if not 0 <= band.payout_percent <= 1:
            return error_result(
                f"payout_percent {band.payout_percent} out of range",
                "INVALID_ARG",
                "Use a fraction between 0.0 and 1.0.",
            )

    try:
        # ใช้ตารางที่ผู้ใช้ส่งมา ถ้าไม่มีจึงใช้ค่าเริ่มต้น
        curve = (
            [band.model_dump() for band in payout_curve]
            if payout_curve is not None
            else [dict(band) for band in DEFAULT_PAYOUT_CURVE]
        )
        # เงื่อนไขแรก: ขนาดแผ่นดินไหวต้องถึงเกณฑ์
        triggered = magnitude >= min_magnitude_trigger
        payouts = []
        for asset in assets:
            distance = haversine_km(lat, lon, asset.latitude, asset.longitude)
            percent = 0.0
            # เงื่อนไขที่สอง: ระยะทางต้องอยู่ในเกณฑ์
            # จากนั้นใช้ช่วงแรกของตารางที่ครอบคลุมระยะนี้
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
                    # ค่าสินไหม = ทุนประกัน × อัตราจ่าย
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
    except Exception:
        log.exception("unexpected failure in compute_parametric_payout")
        raise


# ส่วนที่ 5: จุดตรวจสถานะ ใช้กับ HEALTHCHECK ใน Dockerfile และคำสั่ง curl
@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "seismic-mcp-server", "version": "0.1.0"})


# จุดเริ่มต้นโปรแกรม: เปิดเซิร์ฟเวอร์แบบ Streamable HTTP ที่ /mcp
if __name__ == "__main__":
    log.info("Starting on %s:%s", HOST, PORT)
    log.info("  MCP    : POST http://localhost:%s/mcp", PORT)
    log.info("  Health : GET  http://localhost:%s/health", PORT)
    mcp.run(transport="streamable-http")
