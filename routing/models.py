from django.db import models


class FuelStation(models.Model):
    opis_id = models.IntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    retail_price = models.DecimalField(max_digits=8, decimal_places=6)
    lat = models.FloatField(null=True)
    lng = models.FloatField(null=True)
    geocode_quality = models.CharField(max_length=16)  # city_exact | city_fuzzy | failed
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["state"]),
            models.Index(fields=["geocode_quality"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"
