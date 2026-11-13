from fastapi import APIRouter

from app.services import project_service as svc

router = APIRouter(tags=["key-changes"])


@router.get("/key-changes")
def list_key_changes(
    batch_id: int | None = None,
    stream_id: int | None = None,
    decision: str | None = None,
):
    return svc.list_key_changes(batch_id=batch_id, stream_id=stream_id, decision=decision)


@router.get("/key-changes/overview")
def key_change_overview():
    return svc.key_change_overview()
