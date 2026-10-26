"""Every route the application serves, including those of included routers.

FastAPI keeps an included router as one entry in app.routes (from 0.13x on) instead of copying
its routes there, so tests that enumerate the API walk into each included router. All routers
here are included without a prefix, so a route's path is the path it is served at.
"""


def api_routes(app):
    """Flat list of the route objects of app, in registration order."""
    found = []

    def walk(routes):
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
            else:
                found.append(route)

    walk(app.routes)
    return found
