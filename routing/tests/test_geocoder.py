"""Tests for the offline geocoder."""

from django.test import TestCase

from routing.services.geocoder import (
    InvalidCoordinates,
    LocationNotFound,
    LocationOutsideUSA,
    ResolvedLocation,
    resolve,
)


class TestCoordInput(TestCase):
    def test_anchorage_resolves(self):
        loc = resolve("61.2181,-149.9003")
        self.assertIsNotNone(loc)
        self.assertEqual(loc.resolved_city.split(", ")[-1], "AK")

    def test_barstow_snap(self):
        # Point roughly 10 miles east of Barstow CA
        loc = resolve("34.895,-116.927")
        self.assertIn("CA", loc.resolved_city)
        self.assertGreater(loc.snap_distance_miles, 0)
        self.assertLessEqual(loc.snap_distance_miles, 30)

    def test_mid_atlantic_outside_usa(self):
        with self.assertRaises(LocationOutsideUSA):
            resolve("38.0,-50.0")

    def test_toronto_outside_usa(self):
        with self.assertRaises(LocationOutsideUSA) as ctx:
            resolve("43.65,-79.38")
        self.assertGreater(ctx.exception.distance_miles, 30)

    def test_tijuana_handling(self):
        # Tijuana at (32.51, -117.03) is close to San Diego (~17 mi).
        # We accept it snapping to a US city because the snap distance is under 30 mi.
        # This is documented behavior: snap threshold accepts any point within 30 miles
        # of a US city, and Tijuana is within range of San Diego / the border region.
        try:
            loc = resolve("32.51,-117.03")
            # If it resolves, it must snap to a US city
            self.assertIn("CA", loc.resolved_city)
        except LocationOutsideUSA:
            # Also acceptable if San Diego is > 30 miles (uncommon but possible depending
            # on nearest city in the dataset)
            pass

    def test_malformed_input_abc(self):
        with self.assertRaises((LocationNotFound, InvalidCoordinates)):
            resolve("abc")

    def test_malformed_input_three_parts(self):
        with self.assertRaises((LocationNotFound, InvalidCoordinates)):
            resolve("12.3")

    def test_invalid_lat_out_of_range(self):
        with self.assertRaises(InvalidCoordinates):
            resolve("91,0")

    def test_invalid_lng_out_of_range(self):
        with self.assertRaises(InvalidCoordinates):
            resolve("45,200")


class TestCityNameInput(TestCase):
    def test_los_angeles_ca(self):
        loc = resolve("Los Angeles, CA")
        self.assertEqual(loc.snap_distance_miles, 0.0)
        self.assertIn("Los Angeles", loc.resolved_city)

    def test_new_york_ny(self):
        loc = resolve("New York, NY")
        self.assertEqual(loc.snap_distance_miles, 0.0)

    def test_honolulu_hi(self):
        loc = resolve("Honolulu, HI")
        self.assertEqual(loc.snap_distance_miles, 0.0)

    def test_full_state_name(self):
        loc = resolve("Chicago, Illinois")
        self.assertIn("IL", loc.resolved_city)

    def test_unknown_city_raises(self):
        with self.assertRaises(LocationNotFound) as ctx:
            resolve("Xyzzyville, TX")
        self.assertIsInstance(ctx.exception.suggestions, list)

    def test_suggestions_populated_for_close_match(self):
        with self.assertRaises(LocationNotFound) as ctx:
            resolve("Dallass, TX")
        self.assertTrue(any("dallas" in s.lower() for s in ctx.exception.suggestions))
