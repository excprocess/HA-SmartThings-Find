"""extract_best_location against the shapes Samsung returns, including the ones that used to raise
(and so turned a device Unavailable) or replace a valid position with an empty one."""
from _ha_stubs import Checker, load

extract = load().utils.extract_best_location
check = Checker()

GOOD = {"oprnType": "LOCATION", "latitude": "41.9116", "longitude": "12.6108",
        "horizontalUncertainty": "15.0", "verticalUncertainty": "15.0",
        "extra": {"gpsUtcDt": "20260919013636"}, "oprnDoneDate": "20260919013636"}


def other(**kw):
    entry = {"oprnType": "OFFLINE_LOC", "oprnDoneDate": "20260920090000"}
    entry.update(kw)
    return entry


CASES = {
    "healthy: fresh LOCATION wins over an older OFFLINE_LOC": (
        [GOOD, {"oprnType": "CHECK_CONNECTION", "battery": "24"},
         other(latitude="40.0", longitude="10.0", extra={"gpsUtcDt": "20260918000000"})], (41.9116, 21.2)),
    "newer sighting with only a horizontal uncertainty": (
        [GOOD, other(latitude="45.1", longitude="9.2", horizontalUncertainty="30",
                     extra={"gpsUtcDt": "20260920101010"})], (45.1, 30.0)),
    "newer sighting with empty-string coordinates": (
        [GOOD, other(latitude="", longitude="", extra={"gpsUtcDt": "20260920101010"})], (41.9116, 21.2)),
    "newer sighting with null coordinates": (
        [GOOD, other(latitude=None, longitude=None, extra={"gpsUtcDt": "20260920101010"})], (41.9116, 21.2)),
    "newer entry without any coordinates": (
        [GOOD, other(extra={"gpsUtcDt": "20260920101010"})], (41.9116, 21.2)),
    "encLocation that is an opaque string": ([GOOD, other(encLocation="QUJDREVGRw==")], (41.9116, 21.2)),
    "encLocation that is encrypted": (
        [GOOD, other(encLocation={"encrypted": True, "gpsUtcDt": "20260920101010"})], (41.9116, 21.2)),
    "coordinates but the date only in oprnDoneDate": (
        [GOOD, other(latitude="44.4", longitude="8.9", oprnDoneDate="20260920121212")], (44.4, None)),
    "coordinates with an unparseable date and no fallback": (
        [GOOD, {"oprnType": "OFFLINE_LOC", "latitude": "44.4", "longitude": "8.9",
                "extra": {"gpsUtcDt": "garbage"}}], (41.9116, 21.2)),
    "gpsUtcDt at the top level of the entry": (
        [GOOD, other(latitude="43.3", longitude="7.7", gpsUtcDt="20260921000000")], (43.3, None)),
    "plain nested encLocation": (
        [other(encLocation={"latitude": "46.0", "longitude": "11.0", "gpsUtcDt": "20260921000000",
                            "horizontalUncertainty": "5", "verticalUncertainty": "12"})], (46.0, 13.0)),
    "nested entry with a date but no coordinates": (
        [GOOD, other(encLocation={"gpsUtcDt": "20260921000000"})], (41.9116, 21.2)),
    "only non-location operations": ([{"oprnType": "RING"}, {"oprnType": "CHECK_CONNECTION"}], None),
    "empty, None and junk entries": ([None, "x", 5, {}], None),
    "no operations": ([], None),
    "operations is None": (None, None),
}

for name, (operations, expected) in CASES.items():
    _, location = extract(operations, "dev")
    if expected is None:
        check(name, location, None)
    else:
        check(name, (round(location["latitude"], 4), location["gps_accuracy"]), expected)

check.finish()
