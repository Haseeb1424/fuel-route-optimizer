
import math

DEFAULT_MPG = 10.0
DEFAULT_MAX_RANGE_MILES = 500.0
MAX_STATION_DISTANCE_MILES = 5.0
MIN_STATION_SEPARATION_MILES = 0.25


def haversine_miles(point_a, point_b):
    """Calculate distance between [longitude, latitude] coordinates."""
    lon1, lat1 = point_a
    lon2, lat2 = point_b

    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    dlat = lat2 - lat1
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    return 3958.7613 * 2 * math.asin(
        min(1.0, math.sqrt(a))
    )


def build_route_distances(coordinates):
    """Build cumulative distances along route geometry."""
    cumulative = [0.0]

    for index in range(1, len(coordinates)):
        segment_distance = haversine_miles(
            coordinates[index - 1],
            coordinates[index],
        )
        cumulative.append(cumulative[-1] + segment_distance)

    return cumulative


def project_station_to_route(
    longitude,
    latitude,
    coordinates,
    cumulative,
    reported_route_distance,
):
    """Project a station onto the nearest route segment."""
    if len(coordinates) < 2:
        return None

    geometry_distance = cumulative[-1]
    if geometry_distance <= 0:
        return None

    longitude_scale = 69.172 * max(
        math.cos(math.radians(latitude)),
        0.01,
    )
    latitude_scale = 69.0

    best_distance = math.inf
    best_along_geometry = 0.0

    for index in range(len(coordinates) - 1):
        lon1, lat1 = coordinates[index]
        lon2, lat2 = coordinates[index + 1]

        x1 = (lon1 - longitude) * longitude_scale
        y1 = (lat1 - latitude) * latitude_scale
        x2 = (lon2 - longitude) * longitude_scale
        y2 = (lat2 - latitude) * latitude_scale

        dx = x2 - x1
        dy = y2 - y1
        length_squared = dx * dx + dy * dy

        if length_squared <= 1e-12:
            t = 0.0
        else:
            t = -(x1 * dx + y1 * dy) / length_squared
            t = max(0.0, min(1.0, t))

        nearest_x = x1 + t * dx
        nearest_y = y1 + t * dy
        distance = math.hypot(nearest_x, nearest_y)

        if distance < best_distance:
            best_distance = distance
            segment_length = (
                cumulative[index + 1] - cumulative[index]
            )
            best_along_geometry = (
                cumulative[index] + t * segment_length
            )

    scale = reported_route_distance / geometry_distance

    return {
        "route_mile": best_along_geometry * scale,
        "distance_from_route_miles": best_distance,
    }


def prepare_priced_stations(
    route_geometry,
    route_distance_miles,
    stations,
):
    """Keep confidently price-matched stations near the route."""
    coordinates = route_geometry.get("coordinates", [])

    if len(coordinates) < 2:
        return []

    cumulative = build_route_distances(coordinates)
    unique_stations = {}

    for station in stations:
        price_match = station.get("price_match") or {}

        if price_match.get("status") != "matched":
            continue

        try:
            longitude = float(station["longitude"])
            latitude = float(station["latitude"])
            price = float(station["retail_price"])
        except (KeyError, TypeError, ValueError):
            continue

        if not all(
            math.isfinite(value)
            for value in (longitude, latitude, price)
        ):
            continue

        if not (
            -180 <= longitude <= 180
            and -90 <= latitude <= 90
            and price > 0
        ):
            continue

        projection = project_station_to_route(
            longitude,
            latitude,
            coordinates,
            cumulative,
            route_distance_miles,
        )

        if projection is None:
            continue

        route_mile = projection["route_mile"]
        distance_from_route = (
            projection["distance_from_route_miles"]
        )

        if distance_from_route > MAX_STATION_DISTANCE_MILES:
            continue

        if route_mile <= MIN_STATION_SEPARATION_MILES:
            continue

        if route_mile >= route_distance_miles:
            continue

        candidate = {
            **station,
            "route_mile": round(route_mile, 3),
            "distance_from_route_miles": round(
                distance_from_route, 3
            ),
            "retail_price": price,
            "csv_source_id": price_match.get("source_id"),
        }

        source_id = candidate.get("csv_source_id")
        identity = (
            f"csv:{source_id}"
            if source_id
            else (
                f"place:{candidate.get('place_id') or ''}:"
                f"{longitude:.6f},{latitude:.6f}"
            )
        )

        previous = unique_stations.get(identity)

        if (
            previous is None
            or candidate["distance_from_route_miles"]
            < previous["distance_from_route_miles"]
        ):
            unique_stations[identity] = candidate

    return sorted(
        unique_stations.values(),
        key=lambda station: station["route_mile"],
    )


def _station_summary(station):
    """Return the public details for a recommended fuel stop."""
    return {
        "name": station.get("name"),
        "city": station.get("city"),
        "state": station.get("state"),
        "latitude": station["latitude"],
        "longitude": station["longitude"],
        "place_id": station.get("place_id"),
        "csv_source_id": station.get("csv_source_id"),
        "route_mile": round(station["route_mile"], 2),
        "price_per_gallon": round(station["retail_price"], 4),
    }


def _unavailable(message, estimated_gallons, count=0):
    return {
        "status": "unavailable",
        "message": message,
        "matched_station_count": count,
        "estimated_gallons_consumed": round(
            estimated_gallons, 3
        ),
    }


def optimize_fuel_stops(
    route_geometry,
    route_distance_miles,
    stations,
    mpg=DEFAULT_MPG,
    max_range_miles=DEFAULT_MAX_RANGE_MILES,
):
    """
    Find a feasible, cost-oriented fuel-stop sequence.

    The vehicle starts with a full tank. Its initial fuel purchase
    price is unknown, so the first leg is valued using the first
    selected station's price as a proxy.

    For subsequent legs, the price at the departure station is used.
    The algorithm minimizes this estimated cost first, then the number
    of stops. Only confidently matched CSV prices are considered.
    """
    try:
        distance = float(route_distance_miles)
        mpg = float(mpg)
        max_range = float(max_range_miles)
    except (TypeError, ValueError):
        return _unavailable(
            "Invalid route distance or vehicle assumptions.", 0
        )

    if (
        not all(
            math.isfinite(value)
            for value in (distance, mpg, max_range)
        )
        or distance <= 0
        or mpg <= 0
        or max_range <= 0
    ):
        return _unavailable(
            "Invalid route distance or vehicle assumptions.", 0
        )

    estimated_gallons = distance / mpg

    candidates = prepare_priced_stations(
        route_geometry,
        distance,
        stations,
    )

    if not candidates:
        return _unavailable(
            "No confidently price-matched station was found "
            "within five miles of the route.",
            estimated_gallons,
        )

    # A full tank is sufficient, so no intermediate stop is needed.
    if distance <= max_range:
        cheapest = min(
            candidates,
            key=lambda station: station["retail_price"],
        )

        return {
            "status": "optimized",
            "optimization_method": (
                "No intermediate stops required; estimated fuel "
                "cost uses the lowest confidently matched route price."
            ),
            "route_distance_miles": round(distance, 2),
            "max_range_miles": max_range,
            "miles_per_gallon": mpg,
            "estimated_gallons_consumed": round(
                estimated_gallons, 3
            ),
            "total_fuel_cost_usd": round(
                estimated_gallons * cheapest["retail_price"], 2
            ),
            "fuel_stop_count": 0,
            "fuel_stops": [],
            "matched_station_count": len(candidates),
            "price_estimate_basis": (
                "For a route within one tank, all consumed fuel is "
                "valued at the cheapest matched route price. The "
                "actual starting-tank purchase price is unknown."
            ),
            "lowest_matched_price_station": _station_summary(
                cheapest
            ),
            "assumptions": [
                "Vehicle starts with a full tank.",
                "No intermediate refuelling is required within range.",
                "Fuel cost is an estimate, not a transaction record.",
            ],
        }

    count = len(candidates)

    # Each state is (estimated cost, number of stops).
    # Cost is the primary objective; stop count breaks ties.
    best_states = [(math.inf, math.inf) for _ in range(count)]
    parents = [None] * count

    # Initialize possible first stops. The initial leg is valued at
    # the first selected station's price as a proxy for unknown fuel cost.
    for index, station in enumerate(candidates):
        position = station["route_mile"]

        if position <= max_range:
            best_states[index] = (
                position * station["retail_price"] / mpg,
                1,
            )

    # Dynamic programming: transition between reachable stations.
    for index, station in enumerate(candidates):
        current_cost, current_stop_count = best_states[index]

        if not math.isfinite(current_cost):
            continue

        current_position = station["route_mile"]
        current_price = station["retail_price"]

        for next_index in range(index + 1, count):
            next_station = candidates[next_index]
            next_position = next_station["route_mile"]
            gap = next_position - current_position

            if gap > max_range:
                break

            if gap < MIN_STATION_SEPARATION_MILES:
                continue

            proposed_state = (
                current_cost + gap * current_price / mpg,
                current_stop_count + 1,
            )

            if proposed_state < best_states[next_index]:
                best_states[next_index] = proposed_state
                parents[next_index] = index

    # Choose the cheapest feasible final leg.
    best_index = None
    best_state = (math.inf, math.inf)

    for index, station in enumerate(candidates):
        remaining = distance - station["route_mile"]

        if remaining < 0 or remaining > max_range:
            continue

        current_cost, stop_count = best_states[index]

        if not math.isfinite(current_cost):
            continue

        total_state = (
            current_cost
            + remaining * station["retail_price"] / mpg,
            stop_count,
        )

        if total_state < best_state:
            best_state = total_state
            best_index = index

    if best_index is None:
        return _unavailable(
            "No feasible sequence of confidently price-matched "
            "stations satisfies the maximum-range constraint.",
            estimated_gallons,
            len(candidates),
        )

    path = []
    index = best_index

    while index is not None:
        path.append(index)
        index = parents[index]

    path.reverse()

    recommended_stops = [
        _station_summary(candidates[index])
        for index in path
    ]

    legs = []
    previous_position = 0.0

    for index in path:
        position = candidates[index]["route_mile"]

        legs.append({
            "from_route_mile": round(previous_position, 2),
            "to_route_mile": round(position, 2),
            "distance_miles": round(
                position - previous_position, 2
            ),
            "max_range_miles": max_range,
        })

        previous_position = position

    legs.append({
        "from_route_mile": round(previous_position, 2),
        "to_route_mile": round(distance, 2),
        "distance_miles": round(distance - previous_position, 2),
        "max_range_miles": max_range,
    })

    return {
        "status": "optimized",
        "optimization_method": (
            "Dynamic programming: lowest estimated fuel cost first, "
            "then fewest stops."
        ),
        "route_distance_miles": round(distance, 2),
        "max_range_miles": max_range,
        "miles_per_gallon": mpg,
        "estimated_gallons_consumed": round(
            estimated_gallons, 3
        ),
        "total_fuel_cost_usd": round(best_state[0], 2),
        "fuel_stop_count": len(recommended_stops),
        "fuel_stops": recommended_stops,
        "legs": legs,
        "matched_station_count": len(candidates),
        "price_estimate_basis": (
            "Estimated consumption cost, not actual cash spent at "
            "stops. The initial leg is valued using the first "
            "selected station's price because the starting-tank "
            "purchase price is unknown. Subsequent legs use the "
            "departure station's price."
        ),
        "assumptions": [
            "Vehicle starts with a full tank.",
            "Every leg must be within the maximum vehicle range.",
            "Only confidently matched CSV prices are considered.",
            "Estimated fuel cost is prioritized over stop count.",
            "Fuel cost is an estimate, not a record of actual purchases.",
        ],
    }
