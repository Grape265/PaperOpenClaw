"""Skill endpoints: catalogue, detail, install, enable/disable, uninstall."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, Request, UploadFile

from ...core.errors import BadRequest, SkillNotFound
from ...services import Services
from ..deps import get_services
from ..responses import success
from ..schemas import SkillInstallRequest, SkillPatchRequest

router = APIRouter(tags=["skills"])


@router.get("/skills")
def list_skills(request: Request, services: Services = Depends(get_services)) -> Dict[str, Any]:
    # Re-scan so a skill copied into the directory by hand shows up without a
    # restart; the filesystem is authoritative (FR-9).
    services.skill_loader.scan(register_tools=True, registry=services.agents.registry)
    return success({"items": services.skills.list()}, request)


@router.get("/skills/{skill_id}")
def get_skill(request: Request, skill_id: str,
              services: Services = Depends(get_services)) -> Dict[str, Any]:
    return success(services.skill_loader.detail(skill_id), request)


@router.post("/skills/install")
def install_skill(request: Request, body: SkillInstallRequest,
                  services: Services = Depends(get_services)) -> Dict[str, Any]:
    source = Path(body.path).expanduser()
    if not source.exists():
        raise BadRequest(f"路径不存在：{source}")

    if source.is_dir():
        installed = services.skill_loader.install_from_directory(
            source, enable=body.enable, overwrite=body.overwrite)
    elif source.suffix.lower() == ".zip":
        installed = services.skill_loader.install_from_zip(
            source, enable=body.enable, overwrite=body.overwrite)
    else:
        raise BadRequest("只支持目录或 .zip 压缩包")

    services.agents.reload_skills()
    return success(installed, request)


@router.post("/skills/upload")
async def upload_skill(request: Request,
                       file: UploadFile = File(...),
                       enable: bool = True,
                       overwrite: bool = False,
                       services: Services = Depends(get_services)) -> Dict[str, Any]:
    """Install from an uploaded ``.zip``."""
    name = file.filename or "skill.zip"
    if not name.lower().endswith(".zip"):
        raise BadRequest("请上传 .zip 压缩包")

    data = await file.read()
    if not data:
        raise BadRequest("压缩包为空")

    with tempfile.TemporaryDirectory(prefix="skill_upload_") as tmp:
        archive = Path(tmp) / "skill.zip"
        archive.write_bytes(data)
        installed = services.skill_loader.install_from_zip(
            archive, enable=enable, overwrite=overwrite)
    services.agents.reload_skills()
    return success(installed, request)


@router.patch("/skills/{skill_id}")
def patch_skill(request: Request, skill_id: str, body: SkillPatchRequest,
                services: Services = Depends(get_services)) -> Dict[str, Any]:
    if services.skills.get(skill_id) is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")

    updated: Dict[str, Any] = {}
    if body.enabled is not None:
        updated = services.skill_loader.set_enabled(skill_id, body.enabled)
        services.agents.reload_skills()
    row = services.skills.get(skill_id) or {}
    if body.name or body.description:
        services.db.execute(
            "UPDATE skills SET name = ?, description = ? WHERE id = ?",
            (body.name or row.get("name", ""), body.description or row.get("description", ""),
             skill_id),
        )
        updated = services.skills.get(skill_id) or {}
    return success(updated or row, request)


@router.post("/skills/{skill_id}/reload")
def reload_skill(request: Request, skill_id: str,
                 services: Services = Depends(get_services)) -> Dict[str, Any]:
    row = services.skill_loader.reload(skill_id)
    services.agents.reload_skills()
    return success(row, request)


@router.delete("/skills/{skill_id}")
def delete_skill(request: Request, skill_id: str,
                 services: Services = Depends(get_services)) -> Dict[str, Any]:
    if services.skills.get(skill_id) is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    deleted = services.skill_loader.uninstall(skill_id)
    services.agents.reload_skills()
    return success({"skill_id": skill_id, "deleted": deleted}, request)
