
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from routes.serializers import RouteRequestSerializer
from routes.services.geocoding import (
    GeocodingError,
    geocode_us_location,
)
from routes.services.routing import (
    RoutingError,
    get_driving_route,
)
from routes.services.stations import (
    StationLookupError,
    get_fuel_stations_near_route,
)
from routes.services.prices import match_station_prices
from routes.services.optimization import optimize_fuel_stops


class RoutePreviewView(APIView):
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        start_query = serializer.validated_data["start"]
        finish_query = serializer.validated_data["finish"]

        try:
            start = geocode_us_location(start_query)
            finish = geocode_us_location(finish_query)

            route = get_driving_route(start, finish)

        except GeocodingError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        except RoutingError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        distance_miles = route["distance_miles"]

        try:
            stations = get_fuel_stations_near_route(
                route["geometry"]
            )

            price_result = match_station_prices(stations)
            summary = price_result["summary"]

            station_lookup = {
                "source": "Geoapify Places API",
                "count": summary["total"],
                "price_match_summary": {
                    "matched": summary["matched"],
                    "ambiguous": summary["ambiguous"],
                    "unmatched": summary["unmatched"],
                },
                "stations": price_result["stations"],
            }

            fuel_optimization = optimize_fuel_stops(
                route_geometry=route["geometry"],
                route_distance_miles=distance_miles,
                stations=price_result["stations"],
                mpg=10,
                max_range_miles=500,
            )

        except StationLookupError as exc:
            station_lookup = {
                "source": "Geoapify Places API",
                "count": 0,
                "stations": [],
                "warning": str(exc),
            }

            fuel_optimization = {
                "status": "unavailable",
                "message": (
                    "The route was calculated, but fuel stations "
                    "could not be retrieved."
                ),
            }

        return Response(
            {
                "start": start,
                "finish": finish,
                "route": route,
                "vehicle_assumptions": {
                    "maximum_range_miles": 500,
                    "tank_capacity_gallons": 50,
                    "miles_per_gallon": 10,
                    "estimated_gallons_consumed": round(
                        distance_miles / 10, 2
                    ),
                },
                "fuel_station_candidates": station_lookup,
                "fuel_optimization": fuel_optimization,
                "attribution": (
                    "Map data contributors: "
                    "© OpenStreetMap contributors"
                ),
            },
            status=status.HTTP_200_OK,
        )
