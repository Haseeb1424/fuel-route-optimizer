import csv
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routes.models import FuelStation


US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE",
    "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC",
}


def most_common(values):
    """Return the most frequent non-empty value."""
    cleaned = [str(value).strip() for value in values if str(value).strip()]
    return Counter(cleaned).most_common(1)[0][0] if cleaned else ""


class Command(BaseCommand):
    help = "Import and normalize the supplied fuel-price CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            default=str(
                Path(settings.BASE_DIR)
                / "data"
                / "fuel-prices-for-be-assessment.csv"
            ),
            help="Path to the source CSV file.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"])

        if not csv_path.is_file():
            raise CommandError(f"CSV file not found: {csv_path}")

        grouped = defaultdict(list)
        skipped = 0

        try:
            with csv_path.open(
                "r", encoding="utf-8-sig", newline=""
            ) as source:
                reader = csv.DictReader(source)

                required = {
                    "OPIS Truckstop ID",
                    "Truckstop Name",
                    "Address",
                    "City",
                    "State",
                    "Rack ID",
                    "Retail Price",
                }

                if not required.issubset(set(reader.fieldnames or [])):
                    raise CommandError(
                        "The CSV doesn't contain the expected columns."
                    )

                for row in reader:
                    state = (row["State"] or "").strip().upper()
                    source_id = (row["OPIS Truckstop ID"] or "").strip()

                    # The assessment requests routes inside the USA.
                    if state not in US_STATES or not source_id:
                        skipped += 1
                        continue

                    try:
                        price = Decimal(
                            (row["Retail Price"] or "").strip()
                        )
                    except InvalidOperation:
                        skipped += 1
                        continue

                    if not price.is_finite() or price <= 0:
                        skipped += 1
                        continue

                    row["_state"] = state
                    row["_price"] = price
                    grouped[source_id].append(row)

        except OSError as exc:
            raise CommandError(f"Unable to read CSV: {exc}") from exc

        created = updated = 0

        for source_id, rows in grouped.items():
            # Repeated source IDs can have multiple price observations.
            # With no timestamp, use the median rather than assuming the
            # lowest observation is the current price.
            representative_price = median(
                [row["_price"] for row in rows]
            )

            representative = {
                "name": most_common(
                    [row["Truckstop Name"] for row in rows]
                ),
                "address": most_common(
                    [row["Address"] for row in rows]
                ),
                "city": most_common([row["City"] for row in rows]),
                "state": most_common([row["_state"] for row in rows]),
                "rack_id": most_common(
                    [row["Rack ID"] for row in rows]
                ),
                "retail_price": representative_price,
            }

            _, was_created = FuelStation.objects.update_or_create(
                source_id=source_id,
                defaults=representative,
            )

            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Import complete. Created: {created}; "
                f"updated: {updated}; "
                f"source IDs: {len(grouped)}; "
                f"skipped rows: {skipped}."
            )
        )