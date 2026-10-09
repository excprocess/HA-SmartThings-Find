"""get_fmm_device_location against canned Samsung answers: a read that worked must never end up
marked as failed because of what is in the answer, and real failures must still be reported."""
import asyncio
from unittest.mock import MagicMock

from _ha_stubs import Checker, load

u = load().utils
check = Checker()


class Response:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Client:
    queue: list = []

    def post(self, *args, **kwargs):
        return self.queue.pop(0)


client = Client()


async def fake_cookie(*args, **kwargs):
    return "SESSION", "CSRF"


async def fake_client(*args, **kwargs):
    return client


u.get_web_session_cookie, u._get_web_client = fake_cookie, fake_client


def answer(*operations, **extra):
    body = {"resultCode": "00", "operation": list(operations),
            "lastSelectedDevice": {"telsupport": "N"}, "menu": [{"ring": "N"}]}
    body.update(extra)
    return body


GOOD = {"oprnType": "LOCATION", "latitude": "41.9116", "longitude": "12.6108", "horizontalUncertainty": "15",
        "verticalUncertainty": "15", "extra": {"gpsUtcDt": "20260919013636"}}
SIGHTING = {"oprnType": "OFFLINE_LOC", "latitude": "45.1", "longitude": "9.2", "horizontalUncertainty": "30",
            "extra": {"gpsUtcDt": "20260920101010"}}        # no vertical uncertainty: used to raise
device = {"device_id": "d1", "name": "Watch", "web_dvce_id": "123", "web_usr_id": "u"}
hass = MagicMock()


async def read(*responses, target=device):
    client.queue = list(responses)
    return await u.get_fmm_device_location(hass, MagicMock(), target, "e")


async def main():
    r = await read(Response(200, answer(GOOD)))
    check("normal read", (r["update_success"], r["location_found"], round(r["used_loc"]["latitude"], 4)),
          (True, True, 41.9116))
    check("an old fix is flagged as stale", r["position_stale"], True)

    r = await read(Response(200, answer(GOOD, SIGHTING)))
    check("sighting by nearby devices, horizontal uncertainty only: read succeeds",
          (r["update_success"], r["location_found"]), (True, True))
    check("and its position is the one shown", (r["used_loc"]["latitude"], r["used_loc"]["gps_accuracy"]), (45.1, 30.0))

    r = await read(Response(200, answer({"oprnType": "OFFLINE_LOC", "latitude": "", "longitude": "",
                                         "extra": {"gpsUtcDt": "20260921000000"}})))
    check("an answer holding only an unusable entry still succeeds", (r["update_success"], r["location_found"]), (True, True))
    check("and keeps the last known position", r["used_loc"]["latitude"], 45.1)

    r = await read(Response(200, answer()))
    check("an answer with no operations keeps the last known position",
          (r["update_success"], r["used_loc"]["latitude"]), (True, 45.1))

    r = await read(Response(200, answer(GOOD, lastSelectedDevice=None, menu=None)))
    check("null lastSelectedDevice / menu do not break the read", r["update_success"], True)

    real = u.extract_best_location

    def broken(*args, **kwargs):
        raise RuntimeError("unexpected shape")

    u.extract_best_location = broken
    r = await read(Response(200, answer(GOOD)))
    check("even a bug in the parser does not make the device unavailable",
          (r["update_success"], r["used_loc"]["latitude"]), (True, 41.9116))
    u.extract_best_location = real

    r = await read(Response(200, ["not", "an", "object"]))
    check("a payload that is not an object is a real failure", r["update_success"], False)
    r = await read(Response(200, {"resultCode": "99"}))
    check("Samsung rejecting the request is a real failure", r["update_success"], False)
    r = await read(Response(404, ""), Response(404, ""))
    check("HTTP 404 twice is a real failure", r["update_success"], False)

    fresh = {"device_id": "d2", "name": "New", "web_dvce_id": "9", "web_usr_id": "u"}
    r = await read(Response(200, answer()), target=fresh)
    check("a device with no position yet: succeeds, nothing to show", (r["update_success"], r["location_found"]), (True, False))


asyncio.run(main())
check.finish()
