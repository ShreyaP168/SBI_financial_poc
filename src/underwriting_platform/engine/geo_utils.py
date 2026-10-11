"""
engine/geo_utils.py — pure-stdlib Haversine distance.

Ported verbatim from kaiuw-sbi-prod's geo_utils.py (no logging dependency,
no Google Maps fallback). Used by `engine/identity_verification.py` to
compute straight-line km between applicant residence and the diagnostic
centre once both addresses have been geocoded.

Haversine formula:
  a = sin²(Δlat/2) + cos(lat1) · cos(lat2) · sin²(Δlon/2)
  c = 2 · asin(√a)
  distance = R · c        where R = 6371 km (mean Earth radius)
"""

from __future__ import annotations
import math
from typing import Any, Optional

EARTH_RADIUS_KM = 6371.0  # mean radius of Earth, km


def haversine_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> float:
    """Great-circle distance in km, rounded to 2 decimals.

    Returns 0.0 when any input fails to parse — caller should treat
    a 0.0 with one None coord as "indeterminate", not "same location".
    """
    try:
        lat1_r = math.radians(float(lat1))
        lat2_r = math.radians(float(lat2))
        dlat = lat2_r - lat1_r
        dlon = math.radians(float(lon2) - float(lon1))
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
        c = 2 * math.asin(math.sqrt(a))
        return round(EARTH_RADIUS_KM * c, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def parse_coord(value: Any) -> Optional[float]:
    """Tolerant float parser; rejects 0.0 (treated as missing GPS fix)
    and out-of-range values."""
    if value is None or value == "" or value == "N/A":
        return None
    try:
        f = float(str(value).strip().rstrip("°"))
    except (ValueError, TypeError):
        return None
    if f == 0.0:
        return None
    if not (-180.0 <= f <= 180.0):
        return None
    return f


def coords_available(lat: Any, lon: Any) -> bool:
    """Both lat and lon are non-zero, in-range floats."""
    return parse_coord(lat) is not None and parse_coord(lon) is not None


def distance_band_km(km: float) -> str:
    """Bucket km into the bands the identity prompt declares.

    Bands match `agents/prompts/identity.txt` `dc_distance_band` enum:
      ≤10 km, 10–25 km, 25–50 km, 50–100 km, >100 km, Unknown
    """
    if km is None or km <= 0:
        return "Unknown"
    if km <= 10:
        return "≤10 km"
    if km <= 25:
        return "10–25 km"
    if km <= 50:
        return "25–50 km"
    if km <= 100:
        return "50–100 km"
    return ">100 km"
