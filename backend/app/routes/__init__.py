"""HTTP and WebSocket routers mounted by the FastAPI app.

Nothing is re-exported here on purpose. Importing a router constructs the shared
OpenAlgoClient, so each router is imported by name at the point it is mounted and an
unused one costs nothing.
"""
