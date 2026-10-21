import re
from difflib import SequenceMatcher

from routes.models import FuelStation


STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ",
    "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

GENERIC_WORDS = {
    "travel", "center", "centers", "truck", "stop",
    "stopping", "plaza", "fuel", "food", "mart",
    "store", "stores", "station", "gas", "express",
}


def normalize_text(value):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").casefold(),
    ).strip()


def normalize_state(value):
    state = normalize_text(value)

    if len(state) == 2:
        return state.upper()

    return STATE_CODES.get(state)


def normalize_brand(value):
    """
    Normalize common brand variations without assuming that
    unrelated businesses are the same station.
    """
    name = normalize_text(value)

    aliases = [
        (r"\bpilot\b|\bflying j\b", "pilot"),
        (r"\blove s\b|\bloves\b", "loves"),
        (r"\bcircle k\b", "circle k"),
        (r"\btravelcenters of america\b|\bta\b", "ta"),
        (r"\bkwik trip\b", "kwik trip"),
        (r"\bone9\b", "one9"),
        (r"\bspeedway\b", "speedway"),
        (r"\bchevron\b", "chevron"),
        (r"\bshell\b", "shell"),
        (r"\bexxon\b", "exxon"),
        (r"\bvalero\b", "valero"),
        (r"\bsinclair\b", "sinclair"),
        (r"\bmarathon\b", "marathon"),
        (r"\bsunoco\b", "sunoco"),
        (r"\bkum and go\b", "kum go"),
    ]

    for pattern, canonical in aliases:
        if re.search(pattern, name):
            return canonical

    tokens = [
        token
        for token in name.split()
        if not token.isdigit() and token not in GENERIC_WORDS
    ]

    return " ".join(tokens)


def match_station_prices(stations):
    """
    Attach prices only where a station has an unambiguous
    brand/name and city/state match in the supplied CSV data.
    """
    records = FuelStation.objects.values(
        "source_id",
        "name",
        "city",
        "state",
        "retail_price",
    )

    by_location = {}

    for record in records:
        key = (
            normalize_text(record["city"]),
            normalize_state(record["state"]),
        )

        if not key[0] or not key[1]:
            continue

        by_location.setdefault(key, []).append(record)

    matched = 0
    ambiguous = 0
    unmatched = 0

    for station in stations:
        station["retail_price"] = None
        station["price_match"] = {
            "status": "unmatched",
            "source": "supplied_csv",
        }

        city = normalize_text(station.get("city"))
        state = normalize_state(station.get("state"))
        candidate_brand = normalize_brand(
            station.get("brand") or station.get("name")
        )

        if not city or not state or not candidate_brand:
            unmatched += 1
            continue

        local_records = by_location.get((city, state), [])

        scored = []

        for record in local_records:
            csv_brand = normalize_brand(record["name"])

            if not csv_brand:
                continue

            score = (
                1.0
                if candidate_brand == csv_brand
                else SequenceMatcher(
                    None, candidate_brand, csv_brand
                ).ratio()
            )

            scored.append((score, record))

        if not scored:
            unmatched += 1
            continue

        best_score = max(score for score, _ in scored)

        # Exact brand match preferred; allow very close names only
        # when one record is clearly the strongest match.
        best_records = [
            record
            for score, record in scored
            if abs(score - best_score) < 0.0001
        ]

        if (
            best_score < 0.90
            or len(best_records) != 1
        ):
            station["price_match"] = {
                "status": "ambiguous",
                "source": "supplied_csv",
            }
            ambiguous += 1
            continue

        record = best_records[0]

        station["retail_price"] = float(record["retail_price"])
        station["price_match"] = {
            "status": "matched",
            "source": "supplied_csv",
            "source_id": record["source_id"],
            "matched_name": record["name"],
            "matched_city": record["city"].strip(),
            "matched_state": record["state"],
            "confidence": "high",
        }

        matched += 1

    return {
        "stations": stations,
        "summary": {
            "total": len(stations),
            "matched": matched,
            "ambiguous": ambiguous,
            "unmatched": unmatched,
        },
    }