from django.db import models


class FuelStation(models.Model):
    source_id = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.CharField(max_length=30, blank=True)

    # Price in USD per US gallon, based on the supplied dataset.
    retail_price = models.DecimalField(
        max_digits=8,
        decimal_places=4,
    )

    # The CSV doesn't provide coordinates. We'll enrich these later.
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    geocoding_status = models.CharField(
        max_length=12,
        choices=[
            ("pending", "Pending"),
            ("matched", "Matched"),
            ("no_match", "No match"),
        ],
        default="pending",
        db_index=True,
    )

    geocoding_confidence = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["retail_price", "name"]
        indexes = [
            models.Index(fields=["state", "city"]),
        ]

    def __str__(self):
        return f"{self.name} - {self.city}, {self.state}"