
import math


DEFAULT_MPG = 10.0
DEFAULT_MAX_RANGE_MILES = 500.0
MAX_STATION_DISTANCE_MILES = 5.0
MIN_STATION_SEPARATION_MILES = 0.25


def haversine_miles(point_a, point_b):
    """Distance between two [longitude, latitude] coordinates."""
    lon1, lat1 = point_a
    lon2, lat2 = point_b

    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1

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
    """Calculate cumulative distance along route geometry."""
    cumulative = [0.0]

    for index in range(1, len(coordinates)):
        segment_distance = haversine_miles(
            coordinates[index - 1],
            coordinates[index],
        )
        cumulative.append(
            cumulative[-1] + segment_distance
        )

    return cumulative


def project_station_to_route(
    longitude,
    latitude,
    coordinates,
    cumulative,
    reported_route_distance,
):
    """Find the nearest route segment and approximate route mileage."""
    if len(coordinates) < 2:
        return None

    geometry_distance = cumulative[-1]

    if geometry_distance <= 0:
        return None

    longitude_scale = (
        69.172 * max(
            math.cos(math.radians(latitude)),
            0.01,
        )
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
    """Filter priced stations and calculate their positions along the route."""
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

        if not (
            math.isfinite(longitude)
            and math.isfinite(latitude)
            and math.isfinite(price)
            and -180 <= longitude <= 180
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

        distance_from_route = (
            projection["distance_from_route_miles"]
        )

        if distance_from_route > MAX_STATION_DISTANCE_MILES:
            continue

        candidate = {
            **station,
            "route_mile": round(
                projection["route_mile"], 3
            ),
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
    """Return public details for a station used in cost estimation."""
    return {
        "name": station.get("name"),
        "city": station.get("city"),
        "state": station.get("state"),
        "latitude": station["latitude"],
        "longitude": station["longitude"],
        "place_id": station.get("place_id"),
        "csv_source_id": station.get("csv_source_id"),
        "route_mile": round(station["route_mile"], 2),
        "price_per_gallon": round(
            station["retail_price"], 4
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
    Estimate trip fuel cost and find a feasible, cost-efficient stop sequence.

    Assumptions:
    - The vehicle starts with a full tank.
    - Starting tank capacity is max_range_miles * mpg gallons.
    - A station must be within 5 route miles to be considered.
    - Only stations with confidently matched CSV prices are considered.
    - For trips that fit within one tank, no intermediate stop is required.
    - For longer trips, initial fuel expenditure is estimated using the
      first selected station's price because the starting fill price is
      unknown.
    """
    try:
        distance = float(route_distance_miles)
        mpg = float(mpg)
        max_range = float(max_range_miles)
    except (TypeError, ValueError):
        return {
            "status": "unavailable",
            "message": "Invalid route distance or vehicle assumptions.",
        }

    if (
        not math.isfinite(distance)
        or not math.isfinite(mpg)
        or not math.isfinite(max_range)
        or distance <= 0
        or mpg <= 0
        or max_range <= 0
    ):
        return {
            "status": "unavailable",
            "message": "Invalid route distance or vehicle assumptions.",
        }

    candidates = prepare_priced_stations(
        route_geometry,
        distance,
        stations,
    )

    estimated_gallons = distance / mpg

    if not candidates:
        return {
            "status": "unavailable",
            "message": (
                "No confidently price-matched station was found "
                "within five miles of the route."
            ),
            "matched_station_count": 0,
            "estimated_gallons_consumed": round(
                estimated_gallons, 3
            ),
        }

    # Trips that fit within a full tank do not require an intermediate stop.
    # We can still estimate consumption cost using the cheapest matched
    # price along the route. This is an estimate, not the actual cost of
    # the starting tank, whose purchase price is unknown.
    if distance <= max_range:
        cheapest = min(
            candidates,
            key=lambda station: station["retail_price"],
        )

        estimated_cost = (
            estimated_gallons * cheapest["retail_price"]
        )

        return {
            "status": "optimized",
            "optimization_method": (
                "No intermediate stops required; "
                "fuel cost estimated at the lowest matched route price"
            ),
            "route_distance_miles": round(distance, 2),
            "max_range_miles": max_range,
            "miles_per_gallon": mpg,
            "estimated_gallons_consumed": round(
                estimated_gallons, 3
            ),
            "total_fuel_cost_usd": round(estimated_cost, 2),
            "fuel_stop_count": 0,
            "fuel_stops": [],
            "price_estimate_basis": (
                "All consumed gallons are valued at the cheapest "
                "confidently matched fuel price found along the route. "
                "The actual starting-tank purchase price is unknown."
            ),
            "lowest_matched_price_station": _station_summary(
                cheapest
            ),
            "matched_station_count": len(candidates),
            "assumptions": [
                "Vehicle starts with a full tank.",
                "No intermediate refuelling is necessary when the "
                "route distance is within maximum range.",
                "Fuel cost is an estimate based on matched CSV prices, "
                "not a record of actual transactions.",
            ],
        }

    # For longer trips, any first selected station must be reachable
    # with the starting tank. We do not impose an arbitrary 25-mile limit.
    count = len(candidates)
    costs = [math.inf] * count
    parents = [None] * count

    for index, station in enumerate(candidates):
        position = station["route_mile"]

        if position <= max_range:
            # Attribute starting-tank fuel to this price as an explicit
            # approximation, since the real starting price is unknown.
            costs[index] = (
                position * station["retail_price"] / mpg
            )

    for index, station in enumerate(candidates):
        if not math.isfinite(costs[index]):
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

            proposed_cost = (
                costs[index] + gap * current_price / mpg
            )

            if proposed_cost < costs[next_index]:
                costs[next_index] = proposed_cost
                parents[next_index] = index

    best_index = None
    best_total_cost = math.inf

    for index, station in enumerate(candidates):
        remaining = distance - station["route_mile"]

        if remaining < -0.01 or remaining > max_range:
            continue

        if not math.isfinite(costs[index]):
            continue

        total_cost = (
            costs[index]
            + max(0.0, remaining)
            * station["retail_price"]
            / mpg
        )

        if total_cost < best_total_cost:
            best_total_cost = total_cost
            best_index = index

    if best_index is None:
        return {
            "status": "unavailable",
            "message": (
                "No feasible sequence of confidently price-matched "
                "stations satisfies the maximum-range constraint."
            ),
            "matched_station_count": len(candidates),
            "estimated_gallons_consumed": round(
                estimated_gallons, 3
            ),
        }

    path = []
    index = best_index

    while index is not None:
        path.append(index)
        index = parents[index]

    path.reverse()

    recommended_stops = [
        _station_summary(candidates[station_index])
        for station_index in path
    ]

    # Leg distances are included so the range constraint can be inspected.
    previous_position = 0.0
    legs = []

    for station_index in path:
        station = candidates[station_index]
        position = station["route_mile"]

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
        "distance_miles": round(
            distance - previous_position, 2
        ),
        "max_range_miles": max_range,
    })

    return {
        "status": "optimized",
        "optimization_method": (
            "Dynamic programming over price-matched stations"
        ),
        "route_distance_miles": round(distance, 2),
        "max_range_miles": max_range,
        "miles_per_gallon": mpg,
        "estimated_gallons_consumed": round(
            estimated_gallons, 3
        ),
        "total_fuel_cost_usd": round(best_total_cost, 2),
        "fuel_stop_count": len(recommended_stops),
        "fuel_stops": recommended_stops,
        "legs": legs,
        "matched_station_count": len(candidates),
        "price_estimate_basis": (
            "The starting tank's actual price is unknown. For the "
            "initial segment, the first selected station's price is "
            "used as an approximation."
        ),
        "assumptions": [
            "Vehicle starts with a full tank.",
            "Only confidently price-matched stations are considered.",
            "Every leg must be within the maximum vehicle range.",
            "Fuel expenditure is estimated; it is not an exact record "
            "of purchases made during the trip.",
        ],
    }
