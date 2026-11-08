"""Seismic & Insurance Analytics MCP Server (Streamable HTTP).

Endpoint : http://localhost:8000/mcp
Health   : http://localhost:8000/health
Logs go to stderr only.
"""
# ส่วนที่ 1: นำเข้าไลบรารี
# - httpx: เรียก API ภายนอก
# - FastMCP: เปลี่ยนฟังก์ชัน Python เป็น MCP Tool
# - pydantic: ตรวจรูปแบบข้อมูลที่รับเข้ามา
from __future__ import annotations  # เปิดให้เขียน type hint แบบใหม่ เช่น str | None

import logging  # บันทึก log
import math  # ฟังก์ชันคณิตศาสตร์ ใช้ในสูตร Haversine
import os  # อ่าน environment variable เช่น HOST และ PORT
import sys  # ใช้ sys.stderr เป็นปลายทางของ log
from datetime import datetime, timedelta, timezone  # จัดการวันเวลาและเขตเวลา
from typing import Annotated, Any  # ชนิดข้อมูลสำหรับ type hint

import httpx  # เรียก HTTP แบบ async ใช้กับ USGS และ TMD
from mcp.server.fastmcp import FastMCP  # ตัวสร้าง MCP Server
from pydantic import BaseModel, ConfigDict, Field  # BaseModel สร้างโครงสร้างข้อมูล, Field ใส่คำอธิบายและช่วงค่า
from starlette.requests import Request  # ชนิดของคำขอ HTTP ใช้ใน /health
from starlette.responses import JSONResponse  # ตอบกลับเป็น JSON ใช้ใน /health

# ส่วนที่ 2: ค่าคงที่ของระบบ
# URL ของแหล่งข้อมูล และคีย์ทดลองที่ TMD เปิดให้ใช้สาธารณะ
USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"  # ที่อยู่ API ของ USGS
TMD_URL = "https://data.tmd.go.th/api/DailySeismicEvent/v1/"  # ที่อยู่ API ของ TMD
TMD_PARAMS = {"uid": "api", "ukey": "api12345", "format": "json"}  # คีย์ทดลองสาธารณะของ TMD ตาม spec.md
THAILAND_TZ = timezone(timedelta(hours=7))  # เขตเวลาไทย +07:00 เพราะ TMD แบ่งวันตามเวลาไทย
# ถ้า API ไม่ตอบภายใน 20 วินาที จะถือว่าเรียกไม่สำเร็จ
HTTP_TIMEOUT = 20.0  # หน่วยวินาที

# ตารางจ่ายเงินเริ่มต้น: ยิ่งใกล้จุดศูนย์กลาง ยิ่งจ่ายมาก
# payout_percent เป็นสัดส่วนของทุนประกัน เช่น 0.5 คือ 50%
DEFAULT_PAYOUT_CURVE = [
    {"max_distance_km": 50, "payout_percent": 0.5},  # ไม่เกิน 50 กม. จ่าย 50%
    {"max_distance_km": 100, "payout_percent": 0.25},  # ไม่เกิน 100 กม. จ่าย 25%
    {"max_distance_km": 200, "payout_percent": 0.1},  # ไม่เกิน 200 กม. จ่าย 10%
]

# 0.0.0.0 ทำให้เข้าถึงเซิร์ฟเวอร์จากนอก Docker container ได้
# เปลี่ยนค่าได้ด้วย environment variable ชื่อ HOST และ PORT
HOST = os.getenv("HOST", "0.0.0.0")  # ที่อยู่ที่เซิร์ฟเวอร์รอรับ ค่าเริ่มต้น 0.0.0.0
PORT = int(os.getenv("PORT", "8000"))  # พอร์ต ค่าเริ่มต้น 8000

# ส่ง log ไปที่ stderr เพื่อไม่ให้ปนกับคำตอบของ MCP
logging.basicConfig(
    stream=sys.stderr,  # ส่ง log ไปที่ stderr
    level=logging.INFO,  # แสดง log ระดับ INFO ขึ้นไป
    format="[seismic-mcp] %(message)s",  # ขึ้นต้นทุกบรรทัดด้วย [seismic-mcp]
)
log = logging.getLogger("seismic-mcp")  # ตัวบันทึก log ของเซิร์ฟเวอร์นี้

# สร้าง MCP Server ที่ใช้เก็บ Tool ทั้งหมด
# stateless_http=True: แต่ละคำขอทำงานแยกกัน ไม่ต้องจำ session
# json_response=True: ตอบกลับเป็น JSON
mcp = FastMCP("seismic-mcp-server", host=HOST, port=PORT, stateless_http=True, json_response=True)
mcp._mcp_server.version = "0.1.0"  # ตั้งเวอร์ชันตาม spec เพราะ FastMCP ไม่มีตัวเลือกนี้โดยตรง


# ---------------------------------------------------------------- input models
# โครงสร้างทรัพย์สินสำหรับการจับคู่ ต้องมีรหัสและพิกัด ส่วนทุนประกันไม่บังคับ
# extra="allow" ยอมรับฟิลด์เพิ่มเติมจากที่กำหนด
class MatchAsset(BaseModel):
    model_config = ConfigDict(extra="allow")  # ยอมรับฟิลด์เพิ่มเติม
    asset_id: str  # รหัสทรัพย์สิน (บังคับ)
    name: str | None = None  # ชื่อทรัพย์สิน (ไม่บังคับ)
    latitude: float  # ละติจูด (บังคับ)
    longitude: float  # ลองจิจูด (บังคับ)
    sum_insured: float | None = None  # ทุนประกัน (ไม่บังคับ)


# โครงสร้างทรัพย์สินสำหรับคำนวณเงิน จึงบังคับให้มี sum_insured
class PayoutAsset(MatchAsset):
    sum_insured: float  # ทุนประกัน (บังคับ)


# หนึ่งช่วงของตารางจ่ายเงิน
# extra="forbid" ไม่รับฟิลด์อื่น และ payout_percent ต้องอยู่ระหว่าง 0 ถึง 1
class PayoutBand(BaseModel):
    model_config = ConfigDict(extra="forbid")  # ไม่รับฟิลด์อื่น
    max_distance_km: float  # ระยะทางสูงสุดของช่วงนี้ (กม.)
    # บอกช่วงค่าใน schema และให้ Tool ตรวจเอง เพื่อตอบ error พร้อม hint
    payout_percent: Annotated[float, Field(json_schema_extra={"minimum": 0, "maximum": 1})]


# ------------------------------------------------------------- error handling
# LVL 1 Recoverable: คืน dict ที่มี error, code, hint เพื่อให้ LLM แก้ argument หรือลองใหม่ได้
# LVL 2 Fatal: บันทึก log แล้ว raise เพราะเป็นปัญหาที่ LLM แก้เองไม่ได้
# หน้าที่: สร้าง dict รูปแบบ error ที่ตกลงกันไว้
# รับ: ข้อความ error, code, hint · คืน: {error, code, hint}
def error_result(error: str, code: str, hint: str) -> dict[str, str]:
    return {"error": error, "code": code, "hint": hint}  # รูปแบบเดียวกันทุก Tool


# หน้าที่: exception สำหรับปัญหาตอนเรียก API ภายนอก
# เก็บผลลัพธ์ error ไว้ใน self.result ให้ Tool คืนต่อได้
class UpstreamError(Exception):
    """Recoverable failure while calling an external earthquake API."""

    def __init__(self, error: str, code: str, hint: str) -> None:
        super().__init__(error)  # เก็บข้อความ error แบบ exception ปกติ
        self.result = error_result(error, code, hint)  # เตรียม dict error ไว้ให้ Tool คืนให้ LLM


# --------------------------------------------------------------------- helpers
# แปลงค่าเป็นตัวเลข เพราะ API อาจส่งตัวเลขมาเป็นข้อความ เช่น "4.6"
# ถ้าแปลงไม่ได้จะคืน None แทนการทำให้โปรแกรมหยุด
# รับ: ค่าใดก็ได้ · คืน: float หรือ None
def _number(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)  # ค่าว่างคืน None ไม่เช่นนั้นแปลงเป็น float
    except (TypeError, ValueError):  # แปลงไม่ได้ เช่น "abc"
        return None


# คืนค่าจากคีย์แรกที่มีข้อมูล ใช้เมื่อ API ตั้งชื่อฟิลด์ได้หลายแบบ
# รับ: dict และรายชื่อคีย์ · คืน: ค่าแรกที่ไม่ว่าง หรือ None
def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:  # ไล่ดูทีละคีย์ตามลำดับ
        if item.get(key) not in (None, ""):  # ข้ามคีย์ที่ไม่มีหรือค่าว่าง
            return item[key]
    return None


# คำนวณระยะทางบนผิวโลกจากพิกัด 2 จุดด้วยสูตร Haversine (หน่วยกิโลเมตร)
# 6371.0088 คือรัศมีเฉลี่ยของโลก
# รับ: พิกัด 2 จุด · คืน: ระยะทาง กม.
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)  # แปลงละติจูดเป็นเรเดียน
    d_phi = math.radians(lat2 - lat1)  # ผลต่างละติจูด
    d_lambda = math.radians(lon2 - lon1)  # ผลต่างลองจิจูด
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2  # ส่วนแรกของสูตร Haversine
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))  # คูณรัศมีโลกได้ระยะทาง กม. (min กันค่าเกิน 1 จากการปัดเศษ)


# แปลงข้อมูล USGS ให้เป็นรูปแบบ EarthquakeEvent เดียวกับทุกแหล่งข้อมูล
# รับ: feature 1 รายการจาก USGS · คืน: EarthquakeEvent
def normalize_usgs(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or {}  # ข้อมูลหลัก เช่น เวลา ขนาด สถานที่
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]  # พิกัด [lon, lat, depth]
    # USGS ส่งเวลาเป็นมิลลิวินาที จึงแปลงเป็น ISO8601 แบบ UTC
    epoch_ms = props.get("time")
    time = (  # แปลงเป็น ISO8601 ลงท้าย Z ถ้าไม่มีเวลาให้เป็น None
        datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if epoch_ms is not None
        else None
    )
    return {
        "id": feature.get("id"),  # รหัสเหตุการณ์
        "time": time,  # เวลาแบบ ISO8601
        "place": props.get("place"),  # ชื่อสถานที่
        "magnitude": props.get("mag"),  # ขนาดแผ่นดินไหว
        # GeoJSON เรียงพิกัดเป็น [longitude, latitude, depth]
        "latitude": coords[1],  # ละติจูด อยู่ตำแหน่งที่ 2
        "longitude": coords[0],  # ลองจิจูด อยู่ตำแหน่งที่ 1
        "depth_km": coords[2],  # ความลึก กม.
        "source": "USGS",  # ระบุแหล่งข้อมูล
        "url": props.get("url"),  # ลิงก์หน้าเหตุการณ์บน USGS
        "raw": feature,  # ข้อมูลดิบต้นฉบับ
    }


# จัดเวลาของ TMD ให้เป็น ISO8601 และระบุเขตเวลาไทย +07:00
# รับ: รายการจาก TMD · คืน: เวลา ISO8601 หรือ None
def _tmd_time(item: dict[str, Any]) -> str | None:
    if item.get("DateTime"):  # รูปแบบตาม spec มีเวลา ISO8601 อยู่แล้ว
        return item["DateTime"]
    thai_time = item.get("DateTimeThai")  # รูปแบบจาก API จริง เช่น 2026-11-04 14:52:00.000
    return f'{thai_time.split(".")[0].replace(" ", "T")}+07:00' if thai_time else None  # ตัดเศษวินาที เปลี่ยนช่องว่างเป็น T แล้วต่อท้าย +07:00


# แปลงข้อมูล TMD 1 รายการให้เป็นรูปแบบ EarthquakeEvent
# รับ: รายการจาก TMD · คืน: EarthquakeEvent
def normalize_tmd(item: dict[str, Any]) -> dict[str, Any]:
    """Accept both the spec shape and the live shape, which has no EventID."""
    # API จริงอาจส่งละติจูดและลองจิจูดเป็นข้อความ จึงแปลงเป็นตัวเลขก่อน
    latitude = _number(item.get("Latitude"))
    longitude = _number(item.get("Longitude"))
    event_id = item.get("EventID")  # รูปแบบตาม spec มี EventID
    # API จริงไม่มี EventID จึงสร้างรหัสจากเวลาและพิกัด เพื่อใช้ตัดข้อมูลซ้ำ
    if event_id is None:
        stamp = str(_first(item, "DateTimeUTC", "DateTimeThai") or "").split(".")[0]  # ใช้เวลา UTC ก่อน ถ้าไม่มีใช้เวลาไทย
        event_id = f'{"".join(c for c in stamp if c.isdigit())}-{latitude}-{longitude}'  # เก็บเฉพาะตัวเลข แล้วต่อกับพิกัด
    return {
        "id": f"TMD-{event_id}",  # เติม TMD- ข้างหน้าตาม spec
        "time": _tmd_time(item),  # เวลาแบบ ISO8601
        "place": _first(item, "Location", "OriginThai", "TitleThai"),  # ชื่อสถานที่ รองรับทั้งชื่อฟิลด์ใน spec และ API จริง
        "magnitude": _number(item.get("Magnitude")),  # ขนาดแผ่นดินไหว
        "latitude": latitude,
        "longitude": longitude,
        "depth_km": _number(_first(item, "DepthKm", "Depth")),  # ความลึก กม.
        "source": "TMD_DailySeismicEvent",  # ระบุแหล่งข้อมูล
        "url": _first(item, "URL", "Url", "Link"),  # ลิงก์ ถ้ามี
        "raw": item,  # ข้อมูลดิบต้นฉบับ
    }


# ดึงรายการแผ่นดินไหวจากคำตอบของ TMD
# รองรับทั้งคีย์ใน spec และคีย์ DailyEarthquakes จาก API จริง
# รับ: JSON ทั้งก้อนจาก TMD · คืน: list ของรายการ
def _tmd_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):  # คำตอบผิดรูปแบบ ถือว่าไม่มีข้อมูล
        return []
    items = payload.get("DailySeismicEvent")  # คีย์ตาม spec.md
    if items is None:
        items = payload.get("DailyEarthquakes")  # คีย์ที่ API จริงใช้
    if isinstance(items, dict):  # ถ้ามีรายการเดียวแบบ dict ให้ห่อเป็น list
        return [items]
    return [i for i in items or [] if isinstance(i, dict)]  # เก็บเฉพาะรายการที่เป็น dict


# ฟังก์ชันกลางสำหรับเรียก API ภายนอก
# แปลงปัญหาของ API เป็น UpstreamError ที่มี code และ hint แล้วส่งต่อให้ Tool
# รับ: URL และพารามิเตอร์ · คืน: ข้อมูล JSON
async def _get_json(url: str, params: dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:  # เปิด HTTP client กำหนดเวลารอและให้ตาม redirect
            response = await client.get(url, params=params)  # ส่ง GET ไปที่ API
            # ถ้า API ตอบ 4xx หรือ 5xx จะเกิด HTTPStatusError
            response.raise_for_status()
            return response.json()  # แปลงคำตอบเป็น Python dict
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


# หน้าที่: ดึงข้อมูล TMD ของวันที่ระบุ แล้วแปลงเป็น EarthquakeEvent
# รับ: วันที่ YYYY-MM-DD · คืน: list ของ EarthquakeEvent
async def _fetch_tmd(date: str) -> list[dict[str, Any]]:
    payload = await _get_json(TMD_URL, {**TMD_PARAMS, "Date": date})  # ส่งคีย์ทดลองและวันที่ไปที่ TMD
    events = [normalize_tmd(item) for item in _tmd_items(payload)]  # แปลงทุกรายการเป็น EarthquakeEvent
    # API จริงไม่สนใจ Date และส่งข้อมูลทั้งปี จึงกรองเฉพาะวันที่ต้องการเอง
    return [e for e in events if str(e["time"] or "")[:10] == date]  # เทียบ 10 ตัวแรกของเวลา (YYYY-MM-DD)


# คืนวันเวลาปัจจุบันตามเวลาไทย แม้ container จะใช้เวลา UTC
def _today() -> datetime:
    return datetime.now(THAILAND_TZ)


# ส่วนที่ 3: Tool สำหรับดึงข้อมูล
# @mcp.tool() ลงทะเบียนฟังก์ชันด้านล่างให้เป็น MCP Tool
# FastMCP สร้าง schema จากชื่อฟังก์ชัน type hint, Field และ docstring
# json_schema_extra บอกช่วงค่าให้ LLM เห็น และให้ Tool ตรวจเองเพื่อตอบ hint ได้
# ------------------------------------------------------------------ data tools
# Tool 1: ค้นหาแผ่นดินไหวทั่วโลกจาก USGS
# รับ: ช่วงเวลา ขนาด พิกัด รัศมี จำนวน · คืน: events และ query หรือ error
@mcp.tool()
async def usgs_search_earthquakes(
    starttime: Annotated[str, Field(description="YYYY-MM-DD or ISO8601.")],  # วันเริ่มต้น (บังคับ)
    endtime: Annotated[str, Field(description="YYYY-MM-DD or ISO8601.")],  # วันสิ้นสุด (บังคับ)
    minmagnitude: Annotated[float, Field(description="Minimum magnitude.")] = 4.5,  # ขนาดต่ำสุด ค่าเริ่มต้น 4.5
    maxmagnitude: Annotated[float | None, Field(description="Maximum magnitude, optional.")] = None,  # ขนาดสูงสุด (ไม่บังคับ)
    latitude: Annotated[float | None, Field(description="Center latitude for radius search, optional.")] = None,  # ละติจูดจุดศูนย์กลาง
    longitude: Annotated[float | None, Field(description="Center longitude for radius search, optional.")] = None,  # ลองจิจูดจุดศูนย์กลาง
    maxradiuskm: Annotated[float | None, Field(description="Search radius in kilometers, optional.")] = None,  # รัศมีค้นหา กม.
    limit: Annotated[int, Field(description="Maximum number of events.", json_schema_extra={"minimum": 1, "maximum": 200})] = 20,  # จำนวนสูงสุด 1–200 ค่าเริ่มต้น 20
) -> dict[str, Any]:  # คืนผลเป็น dict
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
    if any(v is not None for v in radius) and any(v is None for v in radius):  # ส่งมาบางค่าแต่ไม่ครบ
        return error_result(
            "latitude, longitude and maxradiuskm must be provided together",
            "INVALID_ARG",
            "Provide all three values or omit all three.",
        )
    # ตรวจล่วงหน้า: limit ต้องอยู่ระหว่าง 1–200 ตาม spec.md
    if not 1 <= limit <= 200:
        return error_result(f"limit {limit} out of range", "INVALID_ARG", "Use a value between 1 and 200.")
    # ตรวจล่วงหน้า: ขนาดต่ำสุดต้องไม่เกินขนาดสูงสุด
    if maxmagnitude is not None and minmagnitude > maxmagnitude:
        return error_result(
            f"minmagnitude {minmagnitude} exceeds maxmagnitude {maxmagnitude}",
            "INVALID_ARG",
            "Use a minmagnitude that is not greater than maxmagnitude.",
        )
    # รวมพารามิเตอร์ที่จะส่งให้ USGS
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
        payload = await _get_json(USGS_URL, {"format": "geojson", **query})  # เรียก USGS แบบ GeoJSON (post-to-use)
        return {"events": [normalize_usgs(f) for f in payload.get("features", [])], "query": query}  # แปลงทุกรายการเป็น EarthquakeEvent
    except UpstreamError as exc:  # API มีปัญหา: คืน error, code, hint ให้ LLM (LVL 1)
        return exc.result
    except Exception:  # ปัญหาที่ไม่คาดคิด (LVL 2)
        log.exception("unexpected failure in usgs_search_earthquakes")  # บันทึกรายละเอียดลง log
        raise  # ส่ง error ต่อ ไม่ปิดบัง


# Tool 2: ดึงแผ่นดินไหวจาก TMD ของวันเดียว
# รับ: date · คืน: events และ query หรือ error
@mcp.tool()
async def tmd_get_daily_earthquakes(
    date: Annotated[str | None, Field(description="YYYY-MM-DD. If omitted, use today's date.")] = None,  # วันที่ (ไม่บังคับ)
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
    # ตรวจล่วงหน้า: วันที่ต้องเป็นรูปแบบ YYYY-MM-DD
    try:
        datetime.strptime(resolved, "%Y-%m-%d")
    except ValueError:
        return error_result(f"date {resolved} is not valid", "INVALID_ARG", "Use the format YYYY-MM-DD.")
    try:
        return {"events": await _fetch_tmd(resolved), "query": {"date": resolved}}  # ดึงข้อมูลของวันนั้น
    except UpstreamError as exc:  # API มีปัญหา (LVL 1)
        return exc.result
    except Exception:  # ปัญหาที่ไม่คาดคิด (LVL 2)
        log.exception("unexpected failure in tmd_get_daily_earthquakes")
        raise


# Tool 3: ดึงแผ่นดินไหวจาก TMD ย้อนหลังหลายวัน
# รับ: days 1–7 · คืน: events เรียงล่าสุดก่อน และ query หรือ error
@mcp.tool()
async def tmd_get_recent_earthquakes(
    days: Annotated[int, Field(description="Number of days back from today (including today).", json_schema_extra={"minimum": 1, "maximum": 7})] = 3,  # จำนวนวัน 1–7 ค่าเริ่มต้น 3
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
    # ตรวจล่วงหน้า: days ต้องอยู่ระหว่าง 1–7 ตาม spec.md
    if not 1 <= days <= 7:
        return error_result(f"days {days} out of range", "INVALID_ARG", "Use a value between 1 and 7.")
    try:
        today = _today()  # วันนี้ตามเวลาไทย
        events: dict[str, dict[str, Any]] = {}  # เก็บเหตุการณ์โดยใช้ id เป็นคีย์
        for offset in range(days):  # วนตามจำนวนวัน 0, 1, 2, ...
            # ไล่วันจากวันนี้ย้อนกลับไป
            date = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            for event in await _fetch_tmd(date):  # ดึงข้อมูล TMD ของวันนั้น
                # เก็บเหตุการณ์ด้วย id เพื่อตัดข้อมูลซ้ำ
                events.setdefault(event["id"], event)
        # เรียงจากเหตุการณ์ล่าสุด
        ordered = sorted(events.values(), key=lambda e: str(e["time"] or ""), reverse=True)
        return {"events": ordered, "query": {"days": days}}  # ส่งผลกลับ
    except UpstreamError as exc:  # API มีปัญหา (LVL 1)
        return exc.result
    except Exception:  # ปัญหาที่ไม่คาดคิด (LVL 2)
        log.exception("unexpected failure in tmd_get_recent_earthquakes")
        raise


# ส่วนที่ 4: Tool สำหรับวิเคราะห์ประกันภัย
# Tool กลุ่มนี้คำนวณจากข้อมูลที่ส่งเข้ามา ไม่ได้เรียก API ภายนอก
# ------------------------------------------------------------- analytics tools
# Tool 4: หาทรัพย์สินที่อยู่ใกล้แผ่นดินไหว
# รับ: events, assets, รัศมี, ขนาดต่ำสุด · คืน: matches และ parameters หรือ error
@mcp.tool()
def match_events_to_assets(
    events: Annotated[list[dict[str, Any]], Field(description="List of EarthquakeEvent objects.")],  # รายการแผ่นดินไหว
    assets: Annotated[list[MatchAsset], Field(description="Insured assets with coordinates.")],  # รายการทรัพย์สิน
    max_distance_km: Annotated[float, Field(description="Max distance to consider an asset affected.")] = 100,  # รัศมี กม. ค่าเริ่มต้น 100
    min_magnitude: Annotated[float, Field(description="Minimum magnitude to consider.")] = 4.5,  # ขนาดต่ำสุด ค่าเริ่มต้น 4.5
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
    # ตรวจล่วงหน้า: รัศมีต้องไม่ติดลบ
    if max_distance_km < 0:
        return error_result(
            f"max_distance_km {max_distance_km} out of range",
            "INVALID_ARG",
            "Use a value of 0 or greater.",
        )
    try:
        # ขั้นที่ 1: เก็บเฉพาะเหตุการณ์ที่มีข้อมูลครบและถึงเกณฑ์ขนาด
        usable = []
        for event in events:  # ไล่ดูทีละเหตุการณ์
            magnitude, lat, lon = (_number(event.get(k)) for k in ("magnitude", "latitude", "longitude"))  # แปลงขนาดและพิกัดเป็นตัวเลข
            if magnitude is not None and lat is not None and lon is not None and magnitude >= min_magnitude:  # ข้อมูลครบและขนาดถึงเกณฑ์
                usable.append((event, lat, lon))  # เก็บไว้ใช้คำนวณ

        # ขั้นที่ 2: คำนวณระยะจากทรัพย์สินแต่ละรายการไปยังแต่ละเหตุการณ์
        matches = []
        for asset in assets:  # ไล่ดูทีละทรัพย์สิน
            hits = []  # เหตุการณ์ที่อยู่ในรัศมีของทรัพย์สินนี้
            for event, lat, lon in usable:
                distance = haversine_km(lat, lon, asset.latitude, asset.longitude)  # ระยะจากจุดศูนย์กลางถึงทรัพย์สิน
                if distance <= max_distance_km:  # อยู่ในรัศมี
                    hits.append({"event": event, "distance_km": round(distance, 2)})  # เก็บเหตุการณ์และระยะทาง ปัดทศนิยม 2 ตำแหน่ง
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
        return {  # ส่งผลกลับตามรูปแบบใน spec.md
            "matches": matches,
            "parameters": {"max_distance_km": max_distance_km, "min_magnitude": min_magnitude},
        }
    except Exception:  # ปัญหาที่ไม่คาดคิด (LVL 2)
        log.exception("unexpected failure in match_events_to_assets")
        raise


# Tool 5: คำนวณค่าสินไหมแบบ parametric
# รับ: event, assets, เกณฑ์ขนาด, เกณฑ์ระยะ, ตารางจ่าย · คืน: การจ่ายรายทรัพย์สินและยอดรวม หรือ error
@mcp.tool()
def compute_parametric_payout(
    event: Annotated[dict[str, Any], Field(description="Single EarthquakeEvent to evaluate.")],  # แผ่นดินไหว 1 เหตุการณ์
    assets: Annotated[list[PayoutAsset], Field(description="Assets with coordinates and sum_insured.")],  # รายการทรัพย์สิน ต้องมีทุนประกัน
    min_magnitude_trigger: Annotated[float, Field(description="Minimum magnitude to trigger any payout.")] = 5.0,  # ขนาดขั้นต่ำที่จ่าย ค่าเริ่มต้น 5.0
    max_distance_km_trigger: Annotated[float, Field(description="Max distance for eligibility.")] = 200,  # ระยะไกลสุดที่จ่าย ค่าเริ่มต้น 200 กม.
    payout_curve: Annotated[  # ตารางจ่ายตามระยะ (ไม่บังคับ)
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
    magnitude, lat, lon = (_number(event.get(k)) for k in ("magnitude", "latitude", "longitude"))  # แปลงขนาดและพิกัดเป็นตัวเลข
    # ตรวจล่วงหน้า: ต้องมีขนาดและพิกัดเป็นตัวเลขก่อนคำนวณ
    if magnitude is None or lat is None or lon is None:
        return error_result(
            "event must include numeric magnitude, latitude and longitude",
            "INVALID_ARG",
            "Pass an EarthquakeEvent returned by an earthquake data tool.",
        )
    # ตรวจล่วงหน้า: ระยะต้องไม่ติดลบ
    if max_distance_km_trigger < 0:
        return error_result(
            f"max_distance_km_trigger {max_distance_km_trigger} out of range",
            "INVALID_ARG",
            "Use a value of 0 or greater.",
        )
    # ตรวจล่วงหน้า: ทุนประกันต้องไม่ติดลบ
    for asset in assets:
        if asset.sum_insured < 0:
            return error_result(
                f"sum_insured of asset {asset.asset_id} is negative",
                "INVALID_ARG",
                "Use a sum_insured of 0 or greater.",
            )
    # ตรวจล่วงหน้า: อัตราจ่ายต้องอยู่ระหว่าง 0–1
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
        payouts = []  # ผลการจ่ายของแต่ละทรัพย์สิน
        for asset in assets:  # ไล่ดูทีละทรัพย์สิน
            distance = haversine_km(lat, lon, asset.latitude, asset.longitude)  # ระยะจากจุดศูนย์กลางถึงทรัพย์สิน
            percent = 0.0  # เริ่มต้นที่ไม่จ่าย
            # เงื่อนไขที่สอง: ระยะทางต้องอยู่ในเกณฑ์
            # จากนั้นใช้ช่วงแรกของตารางที่ครอบคลุมระยะนี้
            if triggered and distance <= max_distance_km_trigger:
                band = next((b for b in curve if distance <= b["max_distance_km"]), None)  # หาช่วงแรกที่ระยะไม่เกิน max_distance_km
                percent = band["payout_percent"] if band else 0.0  # อัตราจ่ายของช่วงนั้น
            payouts.append(
                {
                    "asset_id": asset.asset_id,  # รหัสทรัพย์สิน
                    "name": asset.name,
                    "distance_km": round(distance, 2),  # ระยะทาง กม.
                    "sum_insured": asset.sum_insured,  # ทุนประกัน
                    "payout_percent": percent,  # อัตราจ่าย
                    # ค่าสินไหม = ทุนประกัน × อัตราจ่าย
                    "payout_amount": round(asset.sum_insured * percent, 2),
                }
            )
        return {  # ส่งผลกลับตามรูปแบบใน spec.md
            "event_id": event.get("id"),
            "event_magnitude": magnitude,
            "parameters": {
                "min_magnitude_trigger": min_magnitude_trigger,
                "max_distance_km_trigger": max_distance_km_trigger,
                "payout_curve": curve,
            },
            "asset_payouts": payouts,
            "total_payout": round(sum(p["payout_amount"] for p in payouts), 2),  # ยอดจ่ายรวมทุกทรัพย์สิน
        }
    except Exception:  # ปัญหาที่ไม่คาดคิด (LVL 2)
        log.exception("unexpected failure in compute_parametric_payout")
        raise


# ส่วนที่ 5: จุดตรวจสถานะ ใช้กับ HEALTHCHECK ใน Dockerfile และคำสั่ง curl
@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:  # รับคำขอ GET /health
    return JSONResponse({"status": "ok", "server": "seismic-mcp-server", "version": "0.1.0"})  # ตอบว่าเซิร์ฟเวอร์ทำงานปกติ


# จุดเริ่มต้นโปรแกรม: เปิดเซิร์ฟเวอร์แบบ Streamable HTTP ที่ /mcp
if __name__ == "__main__":
    log.info("Starting on %s:%s", HOST, PORT)  # แสดงที่อยู่และพอร์ตที่เปิด
    log.info("  MCP    : POST http://localhost:%s/mcp", PORT)
    log.info("  Health : GET  http://localhost:%s/health", PORT)
    mcp.run(transport="streamable-http")  # เปิดเซิร์ฟเวอร์ Streamable HTTP ที่ /mcp
