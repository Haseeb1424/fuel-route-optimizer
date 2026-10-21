import hashlib
import threading
import time

import requests
from django.core.cache import cache


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "FuelRouteOptimizer/1.0 (Django backend assessment)"

# Protect the public service from overlapping requests and respect its
# one-request-per-second usage limit.
_request_lock = threading.Lock()
_last_request_at = 0.0


class GeocodingError(Exception):
    pass


def geocode_us_location(query: str) -> dict:
    """
    Resolve a place/address inside the USA to latitude and longitude.

    Results are cached to avoid repeating geocoding requests.
    """
    global _last_request_at

    query = query.strip()
    if not query:
        raise GeocodingError("Location must not be empty.")

    cache_key = "geocode-us:" + hashlib.sha256(
        query.casefold().encode("utf-8")
    ).hexdigest()

    cached = cache.get(cache_key)
    if cached is not None:
        if cached.get("not_found"):
            raise GeocodingError(
                f"Could not find a US location for '{query}'."
            )
        return cached

    params = {
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 1,
        "countrycodes": "us",
    }

    try:
        with _request_lock:
            elapsed = time.monotonic() - _last_request_at
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

            _last_request_at = time.monotonic()

            response = requests.get(
                NOMINATIM_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=(3, 10),
            )
            response.raise_for_status()

    except requests.RequestException as exc:
        raise GeocodingError(
            "The location service is temporarily unavailable."
        ) from exc

    results = response.json()

    if not results:
        cache.set(cache_key, {"not_found": True}, timeout=300)
        raise GeocodingError(
            f"Could not find a US location for '{query}'."
        )

    result = results[0]
    address = result.get("address", {})

    # Don't rely only on the search query to enforce the USA restriction.
    if address.get("country_code", "").lower() != "us":
        raise GeocodingError(
            f"'{query}' did not resolve to a location in the USA."
        )

    location = {
        "query": query,
        "display_name": result["display_name"],
        "latitude": float(result["lat"]),
        "longitude": float(result["lon"]),
    }

    cache.set(cache_key, location, timeout=60 * 60 * 24 * 30)
    return location