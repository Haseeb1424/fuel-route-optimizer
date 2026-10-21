
import os
import re
import time
from decimal import Decimal
from difflib import SequenceMatcher

import requests
from django.core.management.base import BaseCommand, CommandError

from routes.models import FuelStation


GEOCODE_URL = "https://api.geoapify.com/v1/geocode/search"

MIN_CONFIDENCE = 0.70
MIN_NAME_SIMILARITY = 0.60
REQUEST_TIMEOUT = (3, 15)
REQUEST_DELAY_SECONDS = 0.2

STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ",
    "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

GENERIC_WORDS = {
    "travel", "stop", "stopping", "center", "centers", "truck",
    "plaza", "fuel", "mart", "store", "stores", "food",
    "convenience", "gas", "station", "stations", "dba", "the",
    "and",
}


def normalize_name(value):
    """Normalize names but retain words that identify a station brand."""
    value = str(value or "").casefold().replace("’", "'")
    value = re.sub(r"\blove's\b", "loves", value)
    value = re.sub(r"\b\d+\b", " ", value)

    words = re.findall(r"[a-z]+", value)
    return " ".join(
        word for word in words if word not in GENERIC_WORDS
    )


def normalize_state(value):
    value = str(value or "").strip()

    if len(value) == 2 and value.isalpha():
        return value.upper()

    return STATE_CODES.get(value.casefold())


def similarity(left, right):
    left = normalize_name(left)
    right = normalize_name(right)

    if not left or not right:
        return 0.0

    return SequenceMatcher(None, left, right).ratio()


def is_close_city(source_city, result_city):
    source = str(source_city or "").strip().casefold()
    result = str(result_city or "").strip().casefold()

    if not source or not result:
        return False

    return (
        source == result
        or source in result
        or result in source
        or SequenceMatcher(None, source, result).ratio() >= 0.80
    )


def validate_candidate(station, candidate):
    """Accept only plausible US station locations with good name matches."""
    if candidate.get("country_code", "").lower() != "us":
        return None

    result_type = str(candidate.get("result_type", "")).lower()
    if result_type not in {"amenity", "building"}:
        return None

    state = normalize_state(
        candidate.get("state_code") or candidate.get("state")
    )

    if state != normalize_state(station.state):
        return None

    if not is_close_city(station.city, candidate.get("city")):
        return None

    rank = candidate.get("rank") or {}

    try:
        confidence = float(rank["confidence"])
        latitude = float(candidate["lat"])
        longitude = float(candidate["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    if confidence < MIN_CONFIDENCE:
        return None

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    result_name = (
        candidate.get("name")
        or candidate.get("address_line1")
        or ""
    )

    name_score = similarity(station.name, result_name)

    if name_score < MIN_NAME_SIMILARITY:
        return None

    return {
        "latitude": latitude,
        "longitude": longitude,
        "confidence": confidence,
        "name_score": name_score,
        "name": result_name,
        "formatted": candidate.get("formatted", ""),
    }


class Command(BaseCommand):
    help = (
        "Geocode fuel stations individually and save only "
        "high-confidence coordinates."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum number of pending stations to process.",
        )
        parser.add_argument(
            "--retry-no-match",
            action="store_true",
            help="Also retry stations previously marked no_match.",
        )

    def handle(self, *args, **options):
        api_key = os.getenv("GEOAPIFY_API_KEY")

        if not api_key or api_key in {
            "paste-your-api-key-here",
            "YOUR_ACTUAL_GEOAPIFY_KEY",
        }:
            raise CommandError(
                "Set GEOAPIFY_API_KEY to your real API key."
            )

        limit = options["limit"]

        if not 1 <= limit <= 100:
            raise CommandError("--limit must be between 1 and 100.")

        eligible_statuses = ["pending"]

        if options["retry_no_match"]:
            eligible_statuses.append("no_match")

        stations = list(
            FuelStation.objects.filter(
                geocoding_status__in=eligible_statuses
            ).order_by("source_id")[:limit]
        )

        if not stations:
            self.stdout.write(
                self.style.WARNING("No eligible stations found.")
            )
            return

        self.stdout.write(
            f"Processing {len(stations)} stations using direct "
            "Geoapify geocoding requests."
        )

        matched = 0
        rejected = 0

        for index, station in enumerate(stations, start=1):
            # Exclude the CSV's highway-exit/intersection description
            # from the first query. It can lead to a road rather than
            # the named fuel station.
            query = (
                f"{station.name}, {station.city}, "
                f"{station.state}, United States"
            )

            params = {
                "text": query,
                "type": "amenity",
                "filter": "countrycode:us",
                "format": "json",
                "limit": 5,
                "apiKey": api_key,
            }

            try:
                response = requests.get(
                    GEOCODE_URL,
                    params=params,
                    headers={
                        "User-Agent": "FuelRouteOptimizer/1.0"
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                payload = response.json()

            except requests.RequestException as exc:
                raise CommandError(
                    f"Geocoding request failed at station "
                    f"{station.source_id}: {type(exc).__name__}. "
                    "Already saved results remain in the database."
                ) from exc

            except ValueError as exc:
                raise CommandError(
                    f"Invalid geocoding response for "
                    f"station {station.source_id}."
                ) from exc

            results = payload.get("results", [])

            valid = []

            for candidate in results:
                checked = validate_candidate(station, candidate)

                if checked:
                    # Compare the station's name first, then confidence.
                    quality = (
                        checked["name_score"]
                        + 0.25 * checked["confidence"]
                    )
                    valid.append((quality, checked))

            valid.sort(key=lambda item: item[0], reverse=True)

            selected = None

            if valid:
                best_quality, best = valid[0]

                # Avoid choosing randomly between indistinguishable
                # same-brand stations at different locations.
                if len(valid) > 1:
                    second_quality, second = valid[1]

                    similarly_ranked = (
                        abs(best_quality - second_quality) < 0.03
                    )
                    different_locations = (
                        abs(
                            best["latitude"] - second["latitude"]
                        ) > 0.01
                        or abs(
                            best["longitude"] - second["longitude"]
                        ) > 0.01
                    )

                    if not (
                        similarly_ranked and different_locations
                    ):
                        selected = best
                else:
                    selected = best

            if selected:
                station.latitude = Decimal(
                    str(selected["latitude"])
                )
                station.longitude = Decimal(
                    str(selected["longitude"])
                )
                station.geocoding_confidence = Decimal(
                    str(round(selected["confidence"], 3))
                )
                station.geocoding_status = "matched"
                matched += 1

                self.stdout.write(
                    f"[{index}/{len(stations)}] "
                    f"{station.source_id} | {station.name} | "
                    f"MATCHED | {selected['formatted']}"
                )
            else:
                station.latitude = None
                station.longitude = None
                station.geocoding_status = "no_match"
                rejected += 1

                self.stdout.write(
                    f"[{index}/{len(stations)}] "
                    f"{station.source_id} | {station.name} | "
                    "NO RELIABLE MATCH"
                )

            station.save(
                update_fields=[
                    "latitude",
                    "longitude",
                    "geocoding_status",
                    "geocoding_confidence",
                ]
            )

            if index < len(stations):
                time.sleep(REQUEST_DELAY_SECONDS)

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished. Processed: {len(stations)}; "
                f"matched: {matched}; rejected: {rejected}."
            )
        )
