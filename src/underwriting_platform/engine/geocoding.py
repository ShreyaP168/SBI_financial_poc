"""
engine/geocoding.py — Address → (lat, lon) via Google Maps with
JSON file cache.

Ported from kaiuw-sbi-prod's geocoding_service.py with two changes:
  1. JSON file cache instead of SQLAlchemy DB (v9 has no SQL DB by
     default; the cache is a flat file at `engine/.geocode_cache.json`).
  2. PII masking dropped — log lines write the full address, since
     v9 logs already carry case-level data and no separate masking
     standard is in place.

Fallback chain: full address → simplified (drop leading numbers) →
city+state portion → pincode. First hit wins.

When `GOOGLE_MAPS_API_KEY` is unset the module no-ops (returns None),
letting the engine flag stage emit `DC_DISTANCE_NOT_DETERMINED` rather
than crashing.
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional, Tuple
from underwriting_platform.configuration.paths import CACHE_ROOT

logger = logging.getLogger(__name__)

# Runtime state stays outside the installed, read-only package.
_CACHE_PATH = CACHE_ROOT / "geocoding.json"
_CACHE_LOCK = threading.Lock()

_gmaps_client = None  # lazily initialised


# ── Cache helpers ─────────────────────────────────────────────────


def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[geocoding] cache read failed (%s) — starting empty", e)
        return {}


def _save_cache(data: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[geocoding] cache write failed: %s", e)


def _cache_key(address: str) -> str:
    return hashlib.md5(address.strip().lower().encode("utf-8")).hexdigest()


# ── Google Maps client ────────────────────────────────────────────


def _get_client():
    """Lazily build the Google Maps client. Returns None when the key
    is absent — caller treats geocoding as best-effort.
    """
    global _gmaps_client
    if _gmaps_client is not None:
        return _gmaps_client
    api_key = (os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip()
    if not api_key:
        logger.info("[geocoding] GOOGLE_MAPS_API_KEY not set — geocoding disabled")
        return None
    try:
        import googlemaps
    except ImportError:
        logger.warning("[geocoding] `googlemaps` not installed — geocoding disabled")
        return None
    try:
        _gmaps_client = googlemaps.Client(key=api_key, timeout=10)
        return _gmaps_client
    except Exception as e:
        logger.warning("[geocoding] client init failed: %s", e)
        return None


# ── Address shaping ──────────────────────────────────────────────


def _simplify_address(address: str) -> str:
    """Drop leading house/plot numbers and punctuation."""
    simplified = re.sub(r"^[\d\s/\-\.#,]+", "", address).strip(" ,")
    return simplified or address


def _city_state(address: str) -> str:
    """Last 2-3 comma-separated parts — usually city/state in Indian
    addresses."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 2:
        return ", ".join(parts[-2:])
    return address


# ── Single geocode call ──────────────────────────────────────────


class _ClientUnavailable(Exception):
    """Raised when the Google Maps client cannot be built — distinct
    from a clean API-returned 'no result', so the caller can refuse to
    cache the failure as permanent.
    """


def _geocode_single(address: str) -> Optional[Tuple[float, float]]:
    """Geocode a single attempted address. Returns coords on hit, None
    on a *clean* miss (API responded but had no result). Raises
    `_ClientUnavailable` when the client can't be built or the API call
    itself errored — that branch is non-cacheable.
    """
    if not address or not address.strip():
        return None
    client = _get_client()
    if client is None:
        raise _ClientUnavailable("Google Maps client unavailable")
    try:
        results = client.geocode(address.strip(), region="in", language="en")
    except Exception as e:
        logger.info("[geocoding] API error for %r: %s", address[:80], e)
        raise _ClientUnavailable(str(e))
    if not results:
        return None
    loc = results[0]["geometry"]["location"]
    return (round(loc["lat"], 6), round(loc["lng"], 6))


# ── Public API ────────────────────────────────────────────────────


def geocode_address(address: str, pincode: Optional[str] = None) -> Optional[Tuple[float, float]]:
    """Resolve `address` to (lat, lon) with the 4-step fallback chain.

    Caches every attempted address (including failures, with `null`
    coords) so we don't retry hopeless inputs on later cases.
    """
    if not address or not address.strip():
        # Pincode-only path — useful when only a 6-digit pin is on file.
        if pincode:
            return _resolve_pincode(pincode)
        return None

    key = _cache_key(address)
    with _CACHE_LOCK:
        cache = _load_cache()
    hit = cache.get(key)
    if hit is not None:
        if hit.get("lat") is None or hit.get("lon") is None:
            return None
        return (float(hit["lat"]), float(hit["lon"]))

    attempts = [
        ("full", address),
        ("simplified", _simplify_address(address)),
        ("city+state", _city_state(address)),
    ]
    if pincode:
        attempts.append(("pincode", f"{pincode}, India"))

    result: Optional[Tuple[float, float]] = None
    client_unavailable = False
    for label, query in attempts:
        try:
            result = _geocode_single(query)
        except _ClientUnavailable as e:
            # Non-cacheable failure — env / network problem, NOT an API
            # 'no result'. Bail without writing a permanent null hit so
            # the next run can retry once the env is fixed.
            client_unavailable = True
            logger.info("[geocoding] non-cacheable failure on %s: %s", label, e)
            break
        if result:
            logger.info("[geocoding] resolved via %s → %s", label, result)
            break
        logger.info("[geocoding] miss via %s: %r", label, query[:80])

    if client_unavailable:
        return None

    lat, lon = (result[0], result[1]) if result else (None, None)
    with _CACHE_LOCK:
        cache = _load_cache()
        cache[key] = {"address": address, "lat": lat, "lon": lon}
        _save_cache(cache)
    return result


def _resolve_pincode(pincode: str) -> Optional[Tuple[float, float]]:
    """Pincode-only resolution path — caches separately."""
    pin = (pincode or "").strip()
    if not pin:
        return None
    return geocode_address(f"{pin}, India")
