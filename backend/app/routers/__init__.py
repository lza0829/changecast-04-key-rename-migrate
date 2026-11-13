from fastapi import APIRouter

from app.routers import batches, buffer, dashboard, events, key_changes, projections, settings, streams

api = APIRouter(prefix="/api")
api.include_router(dashboard.router)
api.include_router(streams.router)
api.include_router(batches.router)
api.include_router(projections.router)
api.include_router(events.router)
api.include_router(key_changes.router)
api.include_router(buffer.router)
api.include_router(settings.router)
