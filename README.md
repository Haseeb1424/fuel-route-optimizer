
# Fuel Route Optimizer

A Django REST API that calculates driving routes in the United States and estimates fuel costs using a supplied fuel-price dataset.

The API returns route geometry for map rendering, fuel-station candidates near the route, and an estimated refuelling plan constrained by the vehicle's maximum range.

## Features

- Route calculation with OSRM.
- US location geocoding using Nominatim.
- Fuel-station discovery along the route using Geoapify Places.
- Local fuel-price data stored in Django.
- Estimated fuel consumption at 10 MPG.
- Fuel-stop optimization subject to a 500-mile maximum range.
- Caching to reduce repeated external API requests.
- Automated tests for the optimizer and API endpoint.

## Technology

- Python
- Django
- Django REST Framework
- SQLite for local development
- OSRM for routing
- Nominatim for geocoding
- Geoapify Places API for station discovery

## Requirements

- Python compatible with the pinned dependencies in `requirements.txt`.
- A Geoapify API key.
- The supplied fuel-price CSV.

## Setup

Clone the repository and enter its directory:

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd fuel-route-optimizer
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Configure the Geoapify key in your shell:

```bash
export GEOAPIFY_API_KEY="YOUR_ACTUAL_GEOAPIFY_API_KEY"
```

The key must be kept private. Do not commit it to GitHub.

## Import the supplied fuel prices

Create the data directory:

```bash
mkdir -p data
```

Place the supplied file at:

```text
data/fuel-prices-for-be-assessment.csv
```

Initialize the database and import the data:

```bash
python manage.py migrate
python manage.py import_fuel_prices
```

The import command normalizes records by truckstop ID and excludes records outside the supported US state list.

## Run locally

Start Django:

```bash
python manage.py runserver
```

The API is available at:

```text
http://127.0.0.1:8000/api/v1/routes/optimize/
```

## API

### Optimize a route

`POST /api/v1/routes/optimize/`

Request:

```json
{
  "start": "Dallas, Texas, USA",
  "finish": "Houston, Texas, USA"
}
```

Headers:

```text
Content-Type: application/json
```

The response includes:

- Start and finish coordinates.
- Route distance, duration and GeoJSON geometry.
- Vehicle assumptions and estimated fuel consumption.
- Fuel-station candidates and their price-match status.
- Fuel-optimization status, recommended stops, and estimated fuel cost when feasible.

Example response fields:

```json
{
  "vehicle_assumptions": {
    "maximum_range_miles": 500,
    "tank_capacity_gallons": 50,
    "miles_per_gallon": 10
  },
  "fuel_optimization": {
    "status": "optimized",
    "fuel_stop_count": 0,
    "fuel_stops": []
  }
}
```

The example is illustrative; results depend on the selected route and available station data.

### Input validation

The endpoint rejects missing locations and requests in which the start and finish are identical.

## Run tests

Run the automated test suite:

```bash
python manage.py test routes
```

The tests cover short and long routes, infeasible station candidates, ambiguous prices, request validation, and mocked routing or geocoding failures.

## External services and performance

The application uses the following services:

- Nominatim: resolves the start and finish locations.
- OSRM: retrieves the driving route and geometry.
- Geoapify Places: discovers fuel stations near the route.

Successful location and route lookups are cached locally. Fuel-station results are cached by route geometry. Cache entries use Django's configured cache backend; the default local-memory cache is suitable for development but is not shared between multiple workers.

Public API availability, quotas, and rate limits apply. The API may return a route even when station discovery or fuel optimization is unavailable.

## Fuel-cost assumptions and limitations

- Fuel economy is assumed to be 10 miles per US gallon.
- Maximum range is assumed to be 500 miles, implying a nominal 50-gallon tank at the stated fuel economy.
- The optimizer considers stations near the route only when their prices have been confidently matched to the supplied CSV.
- The CSV does not contain station coordinates. External station records must be matched to the local price dataset, and uncertain matches must not be treated as valid prices.
- The CSV does not establish when each price was observed. A stored price is not guaranteed to be current.
- When a trip fits within the assumed range, the API reports no intermediate stops. Its estimated fuel cost uses the cheapest confidently matched price along the route, because the actual purchase price of the starting tank is unknown.
- For longer trips, starting-fuel cost is estimated under explicit assumptions. The result is not a record of actual purchases.
- Sparse or ambiguous station-price matches can prevent the optimizer from returning a feasible plan.
- Route projections and distance calculations are approximations.

## Data attribution

Routing and map data are provided through services using OpenStreetMap data. Retain appropriate OpenStreetMap attribution when displaying map geometry.

## Development notes

Before production deployment:

- Configure a shared cache backend.
- Add structured API logging and monitoring.
- Review external API rate limits and terms.
- Improve and validate fuel-station-to-price matching.
- Add route-level integration tests using representative data.
- Configure production security settings and a production WSGI or ASGI server.
