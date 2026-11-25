"""Loopback-only switches for features in your local app."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from app.api.local_settings import LocalRoute
from app.core.local_capabilities import capability, list_capabilities
from app.core.local_secrets import update_settings
from app.core.providers_local import _local_api

router = APIRouter(prefix="/local/capabilities", tags=["local-capabilities"],
                   route_class=LocalRoute, dependencies=[Depends(_local_api)])


class SwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool


@router.get("")
def get_capabilities():
    return {"capabilities": list_capabilities()}


@router.put("/{name}")
def put_capability(name: str, body: SwitchBody):
    try:
        capability(name)
    except KeyError:
        raise HTTPException(404, "Choose a feature listed in Settings > Capabilities, then try again.") from None
    update_settings("capabilities", {name: body.on}, {})
    return capability(name)
