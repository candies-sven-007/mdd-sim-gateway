"""The API surface is pinned so that moving routes between modules cannot add, drop or rename
one. Change EXPECTED only together with a deliberate API change."""
import unittest

from control.app import main
from tests.route_listing import api_routes

EXPECTED = {
    ('/api/auth/login', 'POST'),
    ('/api/auth/logout', 'POST'),
    ('/api/auth/password', 'POST'),
    ('/api/auth/setup', 'POST'),
    ('/api/auth/status', 'GET'),
    ('/api/cards', 'GET'),
    ('/api/devices', 'GET'),
    ('/api/devices/{device_id}', 'DELETE'),
    ('/api/devices/{device_id}/capabilities', 'PATCH'),
    ('/api/devices/{device_id}/diagnostics', 'POST'),
    ('/api/devices/{device_id}/hardware', 'PUT'),
    ('/api/devices/{device_id}/name', 'PUT'),
    ('/api/devices/{device_id}/sim/reread', 'POST'),
    ('/api/diagnostics/support-bundle', 'GET'),
    ('/api/egress/profile/{profile_id}/test', 'POST'),
    ('/api/egress/refresh', 'POST'),
    ('/api/egress/status', 'GET'),
    ('/api/egress/{country}/test', 'POST'),
    ('/api/engine/event', 'POST'),
    ('/api/esim/chip', 'GET'),
    ('/api/esim/chip/cached', 'GET'),
    ('/api/esim/discovery', 'POST'),
    ('/api/esim/download', 'POST'),
    ('/api/esim/download/cancel', 'POST'),
    ('/api/esim/notifications', 'GET'),
    ('/api/esim/notifications/process', 'POST'),
    ('/api/esim/notifications/{seq}', 'DELETE'),
    ('/api/esim/profiles', 'GET'),
    ('/api/esim/profiles/{iccid}', 'DELETE'),
    ('/api/esim/profiles/{iccid}/disable', 'POST'),
    ('/api/esim/profiles/{iccid}/enable', 'POST'),
    ('/api/esim/profiles/{iccid}/nickname', 'POST'),
    ('/api/esim/status', 'GET'),
    ('/api/instances', 'GET'),
    ('/api/instances', 'POST'),
    ('/api/instances/{iid}', 'DELETE'),
    ('/api/instances/{iid}/availability', 'GET'),
    ('/api/instances/{iid}/call', 'POST'),
    ('/api/instances/{iid}/calls', 'GET'),
    ('/api/instances/{iid}/calls/delete', 'POST'),
    ('/api/instances/{iid}/country', 'PUT'),
    ('/api/instances/{iid}/hangup', 'POST'),
    ('/api/instances/{iid}/logs', 'GET'),
    ('/api/instances/{iid}/pin/clear', 'POST'),
    ('/api/instances/{iid}/register', 'POST'),
    ('/api/instances/{iid}/reprovision', 'POST'),
    ('/api/instances/{iid}/softphone', 'GET'),
    ('/api/instances/{iid}/softphone/ws', 'WEBSOCKET'),
    ('/api/instances/{iid}/start', 'POST'),
    ('/api/instances/{iid}/status', 'GET'),
    ('/api/instances/{iid}/stop', 'POST'),
    ('/api/media', 'GET'),
    ('/api/ports/suggest', 'GET'),
    ('/api/provision', 'POST'),
    ('/api/readers', 'GET'),
    ('/api/settings', 'GET'),
    ('/api/settings', 'PUT'),
    ('/api/sim/change-pin', 'POST'),
    ('/api/sim/detect', 'GET'),
    ('/api/sim/pin-enabled', 'POST'),
    ('/api/sim/verify-pin', 'POST'),
    ('/api/system/backups', 'POST'),
    ('/api/system/backups/{name}', 'DELETE'),
    ('/api/system/host-alerts', 'DELETE'),
    ('/api/system/maintenance', 'POST'),
    ('/api/system/maintenance/restart-progress', 'GET'),
    ('/api/system/status', 'GET'),
    ('/ws', 'WEBSOCKET'),
}


def _routes():
    found = set()
    for route in api_routes(main.app):
        kind = type(route).__name__
        if kind == "APIWebSocketRoute":
            found.add((route.path, "WEBSOCKET"))
        elif kind == "APIRoute" and route.path != "/{full_path:path}":
            found.update((route.path, method) for method in route.methods)
    return found


class RouteSetTests(unittest.TestCase):
    def test_the_api_surface_is_unchanged(self):
        found = _routes()
        self.assertEqual(sorted(found - EXPECTED), [], "unexpected routes")
        self.assertEqual(sorted(EXPECTED - found), [], "missing routes")

    def test_each_route_is_registered_once(self):
        seen = []
        for route in api_routes(main.app):
            kind = type(route).__name__
            if kind in ("APIRoute", "APIWebSocketRoute"):
                for method in sorted(getattr(route, "methods", None) or ["WEBSOCKET"]):
                    seen.append((route.path, method))
        self.assertEqual(len(seen), len(set(seen)))


if __name__ == "__main__":
    unittest.main()

