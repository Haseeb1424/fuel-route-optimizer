import hashlib

import requests
from django.core.cache import cache


OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
METERS_PER_MILE = 1609.344


class RoutingError(Exception):
    pass


def get_driving_route(start: dict, finish: dict) -> dict:
    """
    Fetch one driving route and return its distance, duration and geometry.
    Coordinates are supplied in longitude,latitude order to OSRM.
    """
    coordinates = (
        f"{start['longitude']},{start['latitude']};"
        f"{finish['longitude']},{finish['latitude']}"
    )

    cache_key = "driving-route:" + hashlib.sha256(
        coordinates.encode("utf-8")
    ).hexdigest()

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        response = requests.get(
            f"{OSRM_URL}/{coordinates}",
            params={
                "overview": "full",
                "geometries": "geojson",
                "steps": "false",
                "alternatives": "false",
            },
            timeout=(3, 15),
        )
        response.raise_for_status()
        payload = response.json()

    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(
            "The routing service is temporarily unavailable."
        ) from exc

    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingError(
            "No driving route was found between these locations."
        )

    route = payload["routes"][0]
    distance_miles = route["distance"] / METERS_PER_MILE

    result = {
        "distance_miles": round(distance_miles, 2),
        "duration_minutes": round(route["duration"] / 60, 1),
        "geometry": route["geometry"],
    }

    # Repeated identical routes don't need another OSRM request.
    cache.set(cache_key, result, timeout=60 * 60)
    return result