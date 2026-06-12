"""Management command to load fuel stations from the bundled CSV."""

import re
import unicodedata
from pathlib import Path

import pandas as pd
from django.core.management.base import BaseCommand

from routing.models import FuelStation

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
FUEL_CSV = DATA_DIR / "fuel-prices-for-be-assessment.csv"
USCITIES_CSV = DATA_DIR / "uscities.csv"

CANADIAN_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"
}

PRICE_MIN = 1.0
PRICE_MAX = 10.0


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    # "st." before a space or end-of-string, and standalone "st" → "saint"
    text = re.sub(r"\bst\.(\s|$)", r"saint\1", text)
    text = re.sub(r"\bst\b", "saint", text)
    return text


def _fuzzy_city(city: str) -> str:
    lowered = city.lower()
    # Replace punctuation/hyphens with space so "St.-Louis" becomes "st  louis" not "stlouis"
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered)
    return _normalize(cleaned)


def _build_city_lookup(uscities_csv: Path) -> dict:
    df = pd.read_csv(uscities_csv, dtype=str)
    df = df.dropna(subset=["city", "state_id", "lat", "lng"])
    lookup = {}
    for _, row in df.iterrows():
        city_norm = _normalize(row["city"])
        state = row["state_id"].strip().upper()
        key = (city_norm, state)
        if key not in lookup:
            lookup[key] = (float(row["lat"]), float(row["lng"]))
    return lookup


def _geocode(city: str, state: str, lookup: dict) -> tuple:
    city_norm = _normalize(city)
    key = (city_norm, state)
    if key in lookup:
        lat, lng = lookup[key]
        return lat, lng, "city_exact"

    fuzzy = _fuzzy_city(city)
    key_fuzzy = (fuzzy, state)
    if key_fuzzy in lookup:
        lat, lng = lookup[key_fuzzy]
        return lat, lng, "city_fuzzy"

    return None, None, "failed"


class Command(BaseCommand):
    help = "Load fuel stations from the bundled CSV into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-if-loaded",
            action="store_true",
            help="Exit early if the station count already matches expected.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all existing stations before loading.",
        )

    def handle(self, *args, **options):
        df = pd.read_csv(FUEL_CSV)

        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].str.strip()

        df["State"] = df["State"].str.upper()

        canadian_mask = df["State"].isin(CANADIAN_PROVINCES)
        n_canadian = int(canadian_mask.sum())
        df = df[~canadian_mask]
        self.stdout.write(f"Excluded {n_canadian} Canadian rows.")

        price_mask = (df["Retail Price"] > PRICE_MIN) & (df["Retail Price"] <= PRICE_MAX)
        n_bad_price = int((~price_mask).sum())
        df = df[price_mask]
        if n_bad_price:
            self.stdout.write(f"Excluded {n_bad_price} rows with out-of-range prices.")

        df = df.sort_values("Retail Price").drop_duplicates(
            subset=["OPIS Truckstop ID"], keep="first"
        )
        n_deduped = len(df)

        expected = n_deduped

        if options["skip_if_loaded"]:
            current = FuelStation.objects.count()
            if current >= expected:
                self.stdout.write(
                    f"Already have {current} stations (expected {expected}). Skipping."
                )
                return

        if options["clear"]:
            FuelStation.objects.all().delete()

        city_lookup = _build_city_lookup(USCITIES_CSV)

        quality_counts = {"city_exact": 0, "city_fuzzy": 0, "failed": 0}
        failed_cities = {}

        stations = []
        for _, row in df.iterrows():
            city = str(row["City"])
            state = str(row["State"])
            lat, lng, quality = _geocode(city, state, city_lookup)
            quality_counts[quality] += 1
            if quality == "failed":
                key = (city, state)
                failed_cities[key] = failed_cities.get(key, 0) + 1

            stations.append(
                FuelStation(
                    opis_id=int(row["OPIS Truckstop ID"]),
                    name=str(row["Truckstop Name"]),
                    address=str(row["Address"]),
                    city=city,
                    state=state,
                    retail_price=row["Retail Price"],
                    lat=lat,
                    lng=lng,
                    geocode_quality=quality,
                )
            )

        FuelStation.objects.bulk_create(
            stations,
            batch_size=1000,
            update_conflicts=True,
            unique_fields=["opis_id"],
            update_fields=["name", "address", "city", "state", "retail_price", "lat", "lng", "geocode_quality"],
        )

        total = len(stations)
        exact = quality_counts["city_exact"]
        fuzzy = quality_counts["city_fuzzy"]
        failed = quality_counts["failed"]
        hit_rate = (exact + fuzzy) / total * 100 if total else 0

        self.stdout.write(
            f"\nIngestion summary:\n"
            f"  Total rows in CSV: {len(pd.read_csv(FUEL_CSV))}\n"
            f"  After Canadian exclusion: {total + n_canadian + n_bad_price - n_canadian}\n"
            f"  After dedup: {total}\n"
            f"  Geocode city_exact: {exact}\n"
            f"  Geocode city_fuzzy: {fuzzy}\n"
            f"  Geocode failed: {failed}\n"
            f"  Hit rate: {hit_rate:.1f}%"
        )

        if hit_rate < 95:
            self.stderr.write(
                f"\nWARNING: geocode hit rate {hit_rate:.1f}% is below 95%."
            )
            top = sorted(failed_cities.items(), key=lambda x: -x[1])[:20]
            self.stderr.write("Top unmatched (city, state):")
            for (city, state), cnt in top:
                self.stderr.write(f"  {city}, {state} ({cnt})")
        else:
            self.stdout.write(f"Geocode hit rate OK ({hit_rate:.1f}%).")

        from routing.services.corridor import reload_stations
        reload_stations()
        self.stdout.write("Station corridor cache refreshed.")
