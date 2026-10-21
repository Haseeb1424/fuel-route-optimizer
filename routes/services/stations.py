
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
MAX_PAGES = 3
MAX_ROUTE_POINTS = 800
CACHE_VERSION = "v3"


class StationLookupError(Exception):
    """Raised when fuel-station discovery fails."""


def _simplify_route(coordinates, max_points=MAX_ROUTE_POINTS):
    """
    Evenly sample a GeoJSON LineString for station discovery.

    Preserve both endpoints. The full OSRM geometry is still used when
    computing the actual distance of each station from the route.
    """
    if len(coordinates) <= max_points:
        return coordinates

    if max_points < 2:
        raise ValueError("max_points must be at least 2.")

    last_index = len(coordinates) - 1

    indices = sorted({
        round(index * last_index / (max_points - 1))
        for index in range(max_points)
    })

    return [coordinates[index] for index in indices]


def _distance_to_route_miles(latitude, longitude, coordinates):
    """Approximate nearest distance to a route vertex, in miles."""
    longitude_scale = (
        69.172 * max(
            math.cos(math.radians(latitude)),
            0.01,
        )
    )

    return min(
        math.hypot(
            (float(point[0]) - longitude) * longitude_scale,
            (float(point[1]) - latitude) * 69.0,
        )
        for point in coordinates
    )


def get_fuel_stations_near_route(route_geometry):
    """
    Discover fuel stations near a GeoJSON LineString.

    On a cache miss, this makes one to three Geoapify Places requests.
    Fuel prices are never inferred here.
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

    try:
        simplified_coordinates = _simplify_route(coordinates)
    except (TypeError, ValueError) as exc:
        raise StationLookupError(
            "Unable to simplify route geometry."
        ) from exc

    simplified_geometry = {
        "type": "LineString",
        "coordinates": simplified_coordinates,
    }

    cache_key = "route-fuel-stations:" + hashlib.sha256(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "geometry": simplified_geometry,
                "buffer_meters": SEARCH_BUFFER_METERS,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    cached = cache.get(cache_key)

    if cached is not None:
        return cached

    base_body = {
        "categories": ["service.vehicle.fuel"],
        "filter": {
            "type": "polyline",
            "geometry": simplified_geometry,
            "buffer": SEARCH_BUFFER_METERS,
        },
        "limit": MAX_RESULTS,
        "lang": "en",
    }

    features_by_id = {}

    for page_number in range(MAX_PAGES):
        offset = page_number * MAX_RESULTS

        # For POST requests, send offset in the JSON request body.
        request_body = {
            **base_body,
            "offset": offset,
        }

        try:
            response = requests.post(
                GEOAPIFY_PLACES_URL,
                params={"apiKey": api_key},
                json=request_body,
                headers={"Accept": "application/json"},
                timeout=(3, 25),
            )

            response.raise_for_status()
            payload = response.json()

        except requests.HTTPError as exc:
            error_response = exc.response
            code = (
                error_response.status_code
                if error_response is not None
                else "unknown"
            )
            error_body = (
                error_response.text[:800]
                if error_response is not None
                else ""
            )

            logger.error(
                "Geoapify Places HTTP error %s: %s",
                code,
                error_body,
            )

            if code == 401 or code == 403:
                message = (
                    "Geoapify rejected the API key or access."
                )
            elif code == 429:
                message = (
                    "Geoapify rate limit or usage quota was exceeded."
                )
            elif code == 400 or code == 413:
                message = (
                    "Geoapify rejected the station-search request. "
                    "See the Django terminal for details."
                )
            else:
                message = (
                    f"Geoapify returned HTTP {code}. "
                    "See the Django terminal for details."
                )

            raise StationLookupError(message) from exc

        except requests.Timeout as exc:
            logger.warning(
                "Geoapify station search timed out; "
                "route points=%s, simplified points=%s",
                len(coordinates),
                len(simplified_coordinates),
            )
            raise StationLookupError(
                "Geoapify station search timed out."
            ) from exc

        except requests.RequestException as exc:
            logger.exception("Geoapify network request failed")
            raise StationLookupError(
                "Could not connect to Geoapify."
            ) from exc

        except ValueError as exc:
            logger.exception("Geoapify returned invalid JSON")
            raise StationLookupError(
                "Geoapify returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise StationLookupError(
                "Unexpected Geoapify response format."
            )

        page_features = payload.get("features", [])

        if not isinstance(page_features, list):
            raise StationLookupError(
                "Geoapify response does not contain a valid features list."
            )

        for feature in page_features:
            properties = feature.get("properties", {})
            geometry = feature.get("geometry", {})
            point = geometry.get("coordinates", [])

            if len(point) < 2:
                continue

            longitude = float(point[0])
            latitude = float(point[1])

            place_id = properties.get("place_id")
            identity = place_id or (
                f"{longitude:.6f},{latitude:.6f}"
            )

            features_by_id[identity] = feature

        if len(page_features) < MAX_RESULTS:
            break

    stations_by_id = {}

    for identity, feature in features_by_id.items():
        properties = feature.get("properties", {})
        geometry = feature.get("geometry", {})
        point = geometry.get("coordinates", [])

        if len(point) < 2:
            continue

        longitude = float(point[0])
        latitude = float(point[1])

        name = (
            properties.get("name")
            or properties.get("brand")
            or properties.get("operator")
            or properties.get("address_line1")
        )

        if not name:
            continue

        distance = _distance_to_route_miles(
            latitude,
            longitude,
            coordinates,
        )

        # Allow a little extra room for the simplified search geometry.
        max_distance = (
            SEARCH_BUFFER_METERS / 1609.344
        ) + 0.75

        if distance > max_distance:
            continue

        stations_by_id[identity] = {
            "place_id": identity,
            "name": name,
            "brand": properties.get("brand"),
            "operator": properties.get("operator"),
            "city": properties.get("city"),
            "state": (
                properties.get("state")
                or properties.get("state_code")
            ),
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

    # Cache only a successfully processed response.
    cache.set(cache_key, stations, timeout=60 * 60 * 6)

    logger.info(
        "Geoapify station discovery successful: "
        "route_points=%s, query_points=%s, candidates=%s",
        len(coordinates),
        len(simplified_coordinates),
        len(stations),
    )

    return stations
