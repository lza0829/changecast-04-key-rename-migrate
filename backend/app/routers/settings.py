from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import project_service as svc

router = APIRouter(tags=["settings"])


class SettingBody(BaseModel):
    value: str


@router.get("/settings")
def list_settings():
    return svc.list_settings()


@router.put("/settings/{key}")
def update_setting(key: str, body: SettingBody):
    try:
        return svc.update_setting(key, body.value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
