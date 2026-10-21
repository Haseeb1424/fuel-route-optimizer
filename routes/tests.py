
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIClient

from routes.services.geocoding import GeocodingError
from routes.services.routing import RoutingError
from routes.services.stations import StationLookupError
from routes.services.optimization import optimize_fuel_stops


class FuelOptimizationTests(SimpleTestCase):
    def setUp(self):
        self.short_route = {
            "type": "LineString",
            "coordinates": [
                [-97.0, 32.0],
                [-96.0, 32.0],
            ],
        }

    def make_station(
        self,
        source_id,
        longitude,
        latitude=32.0,
        price=3.25,
        match_status="matched",
    ):
        return {
            "place_id": f"test-{source_id}",
            "name": f"Test Fuel {source_id}",
            "city": "Test City",
            "state": "TX",
            "longitude": longitude,
            "latitude": latitude,
            "retail_price": price,
            "price_match": {
                "status": match_status,
                "source_id": str(source_id),
            },
        }

    def test_short_route_needs_no_intermediate_stops(self):
        result = optimize_fuel_stops(
            route_geometry=self.short_route,
            route_distance_miles=238.48,
            stations=[
                self.make_station(
                    "1001", -96.5, price=3.25
                )
            ],
            mpg=10,
            max_range_miles=500,
        )

        self.assertEqual(result["status"], "optimized")
        self.assertEqual(result["fuel_stop_count"], 0)
        self.assertEqual(result["fuel_stops"], [])
        self.assertAlmostEqual(
            result["estimated_gallons_consumed"],
            23.848,
            places=3,
        )
        self.assertAlmostEqual(
            result["total_fuel_cost_usd"],
            77.51,
            places=2,
        )

    def test_long_route_returns_feasible_fuel_stops(self):
        route = {
            "type": "LineString",
            "coordinates": [
                [-100.0, 32.0],
                [-99.0, 32.0],
            ],
        }

        stations = [
            self.make_station("2001", -99.75, price=2.50),
            self.make_station("2002", -99.35, price=4.00),
            self.make_station("2003", -99.10, price=2.90),
        ]

        result = optimize_fuel_stops(
            route_geometry=route,
            route_distance_miles=800,
            stations=stations,
            mpg=10,
            max_range_miles=500,
        )

        self.assertEqual(result["status"], "optimized")
        self.assertGreater(result["fuel_stop_count"], 0)
        self.assertGreater(result["total_fuel_cost_usd"], 0)

        for leg in result["legs"]:
            self.assertLessEqual(
                leg["distance_miles"],
                500.01,
                msg=f"Infeasible leg: {leg}",
            )

    def test_ambiguous_prices_are_not_used(self):
        station = self.make_station(
            "3001", -96.5, match_status="ambiguous"
        )

        result = optimize_fuel_stops(
            route_geometry=self.short_route,
            route_distance_miles=238.48,
            stations=[station],
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["matched_station_count"], 0)

    def test_stations_far_from_route_are_excluded(self):
        station = self.make_station(
            "4001", -96.5, latitude=33.0
        )

        result = optimize_fuel_stops(
            route_geometry=self.short_route,
            route_distance_miles=238.48,
            stations=[station],
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["matched_station_count"], 0)

    def test_invalid_route_distance_is_rejected(self):
        station = self.make_station("5001", -96.5)

        result = optimize_fuel_stops(
            route_geometry=self.short_route,
            route_distance_miles=-10,
            stations=[station],
        )

        self.assertEqual(result["status"], "unavailable")


class RouteOptimizeAPITests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = "/api/v1/routes/optimize/"

        self.start = {
            "query": "Dallas, Texas, USA",
            "display_name": "Dallas, Texas, United States",
            "latitude": 32.7763,
            "longitude": -96.7969,
        }

        self.finish = {
            "query": "Houston, Texas, USA",
            "display_name": "Houston, Texas, United States",
            "latitude": 29.7589,
            "longitude": -95.3677,
        }

        self.geometry = {
            "type": "LineString",
            "coordinates": [
                [-96.79675, 32.775945],
                [-95.36770, 29.758938],
            ],
        }

        self.route = {
            "distance_miles": 238.48,
            "duration_minutes": 253.4,
            "geometry": self.geometry,
        }

        self.price_result = {
            "summary": {
                "total": 1,
                "matched": 1,
                "ambiguous": 0,
                "unmatched": 0,
            },
            "stations": [],
        }

        self.optimization = {
            "status": "optimized",
            "fuel_stop_count": 0,
            "fuel_stops": [],
            "estimated_gallons_consumed": 23.848,
            "total_fuel_cost_usd": 77.51,
        }

    def test_valid_request_returns_route_and_optimization(self):
        with (
            patch(
                "routes.views.geocode_us_location",
                side_effect=[self.start, self.finish],
            ),
            patch(
                "routes.views.get_driving_route",
                return_value=self.route,
            ),
            patch(
                "routes.views.get_fuel_stations_near_route",
                return_value=[],
            ),
            patch(
                "routes.views.match_station_prices",
                return_value=self.price_result,
            ),
            patch(
                "routes.views.optimize_fuel_stops",
                return_value=self.optimization,
            ),
        ):
            response = self.client.post(
                self.url,
                {
                    "start": "Dallas, Texas, USA",
                    "finish": "Houston, Texas, USA",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("route", response.data)
        self.assertIn("fuel_optimization", response.data)
        self.assertIn("fuel_station_candidates", response.data)

        self.assertEqual(
            response.data["route"]["geometry"]["type"],
            "LineString",
        )
        self.assertEqual(
            response.data["route"]["geometry"]["coordinates"],
            self.geometry["coordinates"],
        )

    def test_same_start_and_finish_returns_400(self):
        response = self.client.post(
            self.url,
            {
                "start": "Dallas, Texas, USA",
                "finish": "Dallas, Texas, USA",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_location_returns_400(self):
        response = self.client.post(
            self.url,
            {"start": "Dallas, Texas, USA"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_geocoding_failure_returns_400(self):
        with patch(
            "routes.views.geocode_us_location",
            side_effect=GeocodingError("Location not found."),
        ):
            response = self.client.post(
                self.url,
                {
                    "start": "Invalid location",
                    "finish": "Houston, Texas, USA",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400)

    def test_routing_failure_returns_502(self):
        with (
            patch(
                "routes.views.geocode_us_location",
                side_effect=[self.start, self.finish],
            ),
            patch(
                "routes.views.get_driving_route",
                side_effect=RoutingError("Routing unavailable."),
            ),
        ):
            response = self.client.post(
                self.url,
                {
                    "start": "Dallas, Texas, USA",
                    "finish": "Houston, Texas, USA",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 502)
