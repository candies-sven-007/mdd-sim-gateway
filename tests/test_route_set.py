"""The API surface is pinned so that moving routes between modules cannot add, drop or rename
one. Change the baseline only together with a deliberate API change.

The baseline was exported from 9fcbe5c, the last commit before main.py was split into routers,
by walking that commit's app.routes (including included routers) on Python 3.12. It holds every
route under /api/ as (method, path), and every WebSocket path. tests/test_route_auth.py
requests each of these signed out.
"""
import unittest

from control.app import main
from tests.route_listing import api_routes

BASELINE_HTTP = frozenset({
    ("DELETE", "/api/devices/{device_id}"),
    ("DELETE", "/api/esim/notifications/{seq}"),
    ("DELETE", "/api/esim/profiles/{iccid}"),
    ("DELETE", "/api/instances/{iid}"),
    ("DELETE", "/api/system/backups/{name}"),
    ("DELETE", "/api/system/host-alerts"),
    ("GET", "/api/auth/status"),
    ("GET", "/api/cards"),
    ("GET", "/api/devices"),
    ("GET", "/api/diagnostics/support-bundle"),
    ("GET", "/api/egress/status"),
    ("GET", "/api/esim/chip"),
    ("GET", "/api/esim/chip/cached"),
    ("GET", "/api/esim/notifications"),
    ("GET", "/api/esim/profiles"),
    ("GET", "/api/esim/status"),
    ("GET", "/api/instances"),
    ("GET", "/api/instances/{iid}/availability"),
    ("GET", "/api/instances/{iid}/calls"),
    ("GET", "/api/instances/{iid}/logs"),
    ("GET", "/api/instances/{iid}/softphone"),
    ("GET", "/api/instances/{iid}/status"),
    ("GET", "/api/media"),
    ("GET", "/api/ports/suggest"),
    ("GET", "/api/readers"),
    ("GET", "/api/settings"),
    ("GET", "/api/sim/detect"),
    ("GET", "/api/system/maintenance/restart-progress"),
    ("GET", "/api/system/status"),
    ("PATCH", "/api/devices/{device_id}/capabilities"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/password"),
    ("POST", "/api/auth/setup"),
    ("POST", "/api/devices/{device_id}/diagnostics"),
    ("POST", "/api/devices/{device_id}/sim/reread"),
    ("POST", "/api/egress/profile/{profile_id}/test"),
    ("POST", "/api/egress/refresh"),
    ("POST", "/api/egress/{country}/test"),
    ("POST", "/api/engine/event"),
    ("POST", "/api/esim/discovery"),
    ("POST", "/api/esim/download"),
    ("POST", "/api/esim/download/cancel"),
    ("POST", "/api/esim/notifications/process"),
    ("POST", "/api/esim/profiles/{iccid}/disable"),
    ("POST", "/api/esim/profiles/{iccid}/enable"),
    ("POST", "/api/esim/profiles/{iccid}/nickname"),
    ("POST", "/api/instances"),
    ("POST", "/api/instances/{iid}/call"),
    ("POST", "/api/instances/{iid}/calls/delete"),
    ("POST", "/api/instances/{iid}/hangup"),
    ("POST", "/api/instances/{iid}/pin/clear"),
    ("POST", "/api/instances/{iid}/register"),
    ("POST", "/api/instances/{iid}/reprovision"),
    ("POST", "/api/instances/{iid}/start"),
    ("POST", "/api/instances/{iid}/stop"),
    ("POST", "/api/provision"),
    ("POST", "/api/sim/change-pin"),
    ("POST", "/api/sim/pin-enabled"),
    ("POST", "/api/sim/verify-pin"),
    ("POST", "/api/system/backups"),
    ("POST", "/api/system/maintenance"),
    ("PUT", "/api/devices/{device_id}/hardware"),
    ("PUT", "/api/devices/{device_id}/name"),
    ("PUT", "/api/instances/{iid}/country"),
    ("PUT", "/api/settings"),
})

# Deliberate additions after the baseline, each with the change that made it. Every one of
# them is requested signed out by tests/test_route_auth.py like the baseline.
ADDED_HTTP = frozenset({
    # Bluetooth speaker page (docs/SLIM_PLAN.md progress log, 2026-10-28).
    ("GET", "/api/bluetooth/status"),
    ("POST", "/api/bluetooth/scan"),
    ("POST", "/api/bluetooth/devices/{mac}/{action}"),
    ("PUT", "/api/bluetooth/volume"),
    ("PUT", "/api/bluetooth/autoconnect"),
})
SERVED_HTTP = BASELINE_HTTP | ADDED_HTTP

BASELINE_WEBSOCKET = frozenset({
    "/api/instances/{iid}/softphone/ws",
    "/ws",
})

# FastAPI's own documentation routes; disabled (P-1), listed so a return is noticed.
FRAMEWORK_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"})
SPA_PATH = "/{full_path:path}"


def current_routes(app=main.app):
    """(http, websocket, other) as served now.

    Anything that is neither an APIRoute nor an APIWebSocketRoute -- a plain Starlette route or a
    mount -- lands in ``other`` with its type name, so it cannot slip past the comparison.
    """
    http, websocket, other = set(), set(), set()
    for route in api_routes(app):
        kind = type(route).__name__
        path = getattr(route, "path", "")
        if kind == "APIWebSocketRoute":
            websocket.add(path)
        elif kind == "APIRoute" and path != SPA_PATH:
            http.update((method, path) for method in route.methods)
        elif path in FRAMEWORK_PATHS or (kind == "Mount" and path == "/assets") or \
                path == SPA_PATH:
            continue
        else:
            other.add((kind, path))
    return http, websocket, other


class RouteSetTests(unittest.TestCase):
    def test_http_routes_match_the_baseline(self):
        http, _, _ = current_routes()
        self.assertEqual(sorted(http - SERVED_HTTP), [], "routes not in the baseline")
        self.assertEqual(sorted(SERVED_HTTP - http), [], "baseline routes that are gone")

    def test_websocket_paths_match_the_baseline(self):
        _, websocket, _ = current_routes()
        self.assertEqual(websocket, set(BASELINE_WEBSOCKET))

    def test_no_route_of_another_kind(self):
        _, _, other = current_routes()
        self.assertEqual(other, set())

    def test_framework_documentation_is_not_served(self):
        # SLIM_PLAN P-1: outside /api/ the gate lets anyone through.
        paths = {getattr(route, "path", "") for route in api_routes(main.app)}
        self.assertEqual(paths & FRAMEWORK_PATHS, set())
        self.assertIsNone(main.app.openapi_url)
        self.assertIsNone(main.app.docs_url)
        self.assertIsNone(main.app.redoc_url)

    def test_every_http_route_is_under_api(self):
        self.assertTrue(all(path.startswith("/api/") for _, path in SERVED_HTTP))

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
