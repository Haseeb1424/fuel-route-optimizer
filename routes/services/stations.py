import hashlib
import json
import logging
import math
import os

import requests
from django.core.cache import cache


logger = logging.getLogger(__name__)

GEOAPIFY_PLACES_URL = "https://api.geoapify.com/v2/places"

SEARCH_BUFFER_METERS = 5_000
MAX_RESULTS = 500


class StationLookupError(Exception):
    """Raised when route-based station discovery fails."""


def _distance_to_route_miles(latitude, longitude, coordinates):
    """
    Approximate distance to the nearest route vertex.

    Coordinates are GeoJSON [longitude, latitude] pairs.
    """
    longitude_scale = 69.172 * max(
        math.cos(math.radians(latitude)),
        0.01,
    )

    return min(
        math.hypot(
            (route_lon - longitude) * longitude_scale,
            (route_lat - latitude) * 69.0,
        )
        for route_lon, route_lat in coordinates
    )


def get_fuel_stations_near_route(route_geometry):
    """
    Discover fuel stations along a GeoJSON LineString.

    One Geoapify Places request is made on a cache miss.
    Fuel prices are not inferred or invented here.
    """
    api_key = os.getenv("GEOAPIFY_API_KEY")

    if not api_key:
        raise StationLookupError(
            "GEOAPIFY_API_KEY is not configured."
        )

    if (
        not isinstance(route_geometry, dict)
        or route_geometry.get("type") != "LineString"
    ):
        raise StationLookupError(
            "Expected a GeoJSON LineString route."
        )

    coordinates = route_geometry.get("coordinates", [])

    if len(coordinates) < 2:
        raise StationLookupError(
            "Route geometry is missing or invalid."
        )

    geometry_json = json.dumps(
        route_geometry,
        sort_keys=True,
        separators=(",", ":"),
    )

    cache_key = "route-fuel-stations:v2:" + hashlib.sha256(
        geometry_json.encode("utf-8")
    ).hexdigest()

    cached = cache.get(cache_key)

    if cached is not None:
        return cached

    body = {
        "categories": ["service.vehicle.fuel"],
        "filter": {
            "type": "polyline",
            "geometry": route_geometry,
            "buffer": SEARCH_BUFFER_METERS,
        },
        "limit": MAX_RESULTS,
        "lang": "en",
    }

    all_features = {}

    try:
        for offset in (0, 500, 1000):
            response = requests.post(
                GEOAPIFY_PLACES_URL,
                params={
                    "apiKey": api_key,
                    "offset": offset,
                },
                json=body,
                headers={"Accept": "application/json"},
                timeout=(3, 20),
            )

            response.raise_for_status()
            payload = response.json()
            page_features = payload.get("features", [])

            if not isinstance(page_features, list):
                raise StationLookupError(
                    "Unexpected Geoapify response format."
                )

            for feature in page_features:
                properties = feature.get("properties", {})
                geometry = feature.get("geometry", {})
                point = geometry.get("coordinates", [])

                if len(point) < 2:
                    continue

                place_id = properties.get("place_id")
                identity = place_id or (
                    f"{float(point[0]):.6f},"
                    f"{float(point[1]):.6f}"
                )

                all_features[identity] = feature

            # A short page means there are no more results to fetch.
            if len(page_features) < MAX_RESULTS:
                break

    except requests.RequestException as exc:
        logger.warning(
            "Geoapify station lookup failed: %s",
            type(exc).__name__,
        )
        raise StationLookupError(
            "The fuel-station service is temporarily unavailable."
        ) from exc

    except ValueError as exc:
        raise StationLookupError(
            "The fuel-station service returned invalid JSON."
        ) from exc

    features = list(all_features.values())

    if not isinstance(features, list):
        raise StationLookupError(
            "Unexpected response from the fuel-station service."
        )

    stations_by_id = {}

    for feature in features:
        properties = feature.get("properties", {})
        geometry = feature.get("geometry", {})

        point = geometry.get("coordinates", [])

        if len(point) < 2:
            continue

        longitude, latitude = float(point[0]), float(point[1])

        distance = _distance_to_route_miles(
            latitude,
            longitude,
            coordinates,
        )

        station_id = properties.get("place_id")

        if not station_id:
            station_id = f"{longitude:.6f},{latitude:.6f}"

        stations_by_id[station_id] = {
            "place_id": station_id,
            "name": (
                properties.get("name")
                or properties.get("address_line1")
                or "Unnamed fuel station"
            ),
            "brand": properties.get("brand"),
            "operator": properties.get("operator"),
            "city": properties.get("city"),
            "state": properties.get("state"),
            "formatted_address": properties.get("formatted"),
            "latitude": latitude,
            "longitude": longitude,
            "distance_from_route_miles": round(distance, 2),
            "retail_price": None,
        }

    stations = sorted(
        stations_by_id.values(),
        key=lambda station: station["distance_from_route_miles"],
    )

    cache.set(cache_key, stations, timeout=60 * 60 * 6)

    return stations