"""The package version and the HTTP API version (a leaf module, so every part can report them without importing the
API). A server and its clients speak the same API version, or refuse each other (client/remote.py, server/app.py)."""

__version__ = "0.15.0"
API = 3  # the HTTP API lives under /api/v<API>/; 0.14 spoke /api/v2/
API_PREFIX = f"/api/v{API}"
