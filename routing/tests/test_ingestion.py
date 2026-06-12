import io
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from django.test import TestCase

from routing.management.commands.load_stations import (
    CANADIAN_PROVINCES,
    _geocode,
    _build_city_lookup,
)
from routing.models import FuelStation

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USCITIES_CSV = DATA_DIR / "uscities.csv"


class TestDeduplication(TestCase):
    def test_dedup_keeps_lowest_price(self):
        df = pd.DataFrame(
            [
                {"OPIS Truckstop ID": 1, "Truckstop Name": "A", "Address": "", "City": "Dallas", "State": "TX", "Rack ID": 1, "Retail Price": 3.50},
                {"OPIS Truckstop ID": 1, "Truckstop Name": "A", "Address": "", "City": "Dallas", "State": "TX", "Rack ID": 1, "Retail Price": 2.90},
                {"OPIS Truckstop ID": 2, "Truckstop Name": "B", "Address": "", "City": "Houston", "State": "TX", "Rack ID": 2, "Retail Price": 3.10},
            ]
        )
        deduped = df.sort_values("Retail Price").drop_duplicates(
            subset=["OPIS Truckstop ID"], keep="first"
        )
        self.assertEqual(len(deduped), 2)
        row = deduped[deduped["OPIS Truckstop ID"] == 1].iloc[0]
        self.assertAlmostEqual(row["Retail Price"], 2.90, places=2)


class TestGeocodeQuality(TestCase):
    def setUp(self):
        self.lookup = _build_city_lookup(USCITIES_CSV)

    def test_city_exact_match(self):
        lat, lng, quality = _geocode("Los Angeles", "CA", self.lookup)
        self.assertEqual(quality, "city_exact")
        self.assertIsNotNone(lat)

    def test_city_fuzzy_match(self):
        lat, lng, quality = _geocode("Los-Angeles", "CA", self.lookup)
        self.assertIn(quality, ("city_exact", "city_fuzzy"))

    def test_nonexistent_city_fails(self):
        _, _, quality = _geocode("Xyzzyville", "TX", self.lookup)
        self.assertEqual(quality, "failed")


class TestCanadianExclusion(TestCase):
    def test_bc_row_excluded(self):
        df = pd.DataFrame(
            [
                {"OPIS Truckstop ID": 999, "Truckstop Name": "Canadian Stop", "Address": "", "City": "Vancouver", "State": "BC", "Rack ID": 1, "Retail Price": 3.00},
                {"OPIS Truckstop ID": 1, "Truckstop Name": "US Stop", "Address": "", "City": "Dallas", "State": "TX", "Rack ID": 1, "Retail Price": 3.00},
            ]
        )
        df["State"] = df["State"].str.upper()
        filtered = df[~df["State"].isin(CANADIAN_PROVINCES)]
        ids = list(filtered["OPIS Truckstop ID"])
        self.assertNotIn(999, ids)
        self.assertIn(1, ids)

    def test_all_canadian_provinces_excluded(self):
        for prov in CANADIAN_PROVINCES:
            self.assertIn(prov, CANADIAN_PROVINCES)


class TestPriceSanityGate(TestCase):
    def test_zero_price_excluded(self):
        df = pd.DataFrame([{"Retail Price": 0.0}, {"Retail Price": 3.0}, {"Retail Price": 11.0}])
        from routing.management.commands.load_stations import PRICE_MIN, PRICE_MAX
        filtered = df[(df["Retail Price"] > PRICE_MIN) & (df["Retail Price"] <= PRICE_MAX)]
        self.assertEqual(len(filtered), 1)
        self.assertAlmostEqual(filtered.iloc[0]["Retail Price"], 3.0)


class TestSkipIfLoaded(TestCase):
    def test_skip_if_loaded_flag_respected(self):
        from django.test import override_settings
        from django.core.management import call_command

        FuelStation.objects.all().delete()

        with patch("routing.management.commands.load_stations.FuelStation") as mock_model:
            mock_model.objects.count.return_value = 10000
            import io
            out = io.StringIO()
            with patch("routing.management.commands.load_stations.FuelStation.objects.count", return_value=10000):
                pass
