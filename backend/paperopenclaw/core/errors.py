"""Typed application errors mapped to API error codes.

Every error carries a stable ``code`` (see docs/03-api-contract.md section 0.2)
so the frontend can switch on it instead of parsing messages.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AppError(Exception):
    """Base class for all expected, user-facing failures."""

    code = "INTERNAL_ERROR"
    http_status = 500
    default_message = "内部错误"

    def __init__(self, message: Optional[str] = None, **details: Any) -> None:
        self.message = message or self.default_message
        self.details: Dict[str, Any] = details
        super().__init__(self.message)

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class BadRequest(AppError):
    code = "BAD_REQUEST"
    http_status = 400
    default_message = "参数不合法"


class Unauthorized(AppError):
    code = "UNAUTHORIZED"
    http_status = 401
    default_message = "未授权"


class PaperNotFound(AppError):
    code = "PAPER_NOT_FOUND"
    http_status = 404
    default_message = "论文不存在"


class JobNotFound(AppError):
    code = "JOB_NOT_FOUND"
    http_status = 404
    default_message = "任务不存在"


class SessionNotFound(AppError):
    code = "SESSION_NOT_FOUND"
    http_status = 404
    default_message = "会话不存在"


class SkillNotFound(AppError):
    code = "SKILL_NOT_FOUND"
    http_status = 404
    default_message = "技能不存在"


class AssetNotFound(AppError):
    code = "ASSET_NOT_FOUND"
    http_status = 404
    default_message = "图表不存在"


class ConceptNotFound(AppError):
    code = "CONCEPT_NOT_FOUND"
    http_status = 404
    default_message = "OKF 概念不存在"


class PathOutOfScope(AppError):
    code = "PATH_OUT_OF_SCOPE"
    http_status = 400
    default_message = "路径越出数据目录"


class DuplicatePaper(AppError):
    code = "DUPLICATE_PAPER"
    http_status = 409
    default_message = "论文已存在"

    def __init__(self, hash_value: str, title: str = "", **details: Any) -> None:
        super().__init__(f"论文已存在：{title or hash_value}", hash=hash_value, **details)


class ProviderNotConfigured(AppError):
    code = "PROVIDER_NOT_CONFIGURED"
    http_status = 412
    default_message = "大模型提供方未配置"


class ProviderError(AppError):
    code = "PROVIDER_ERROR"
    http_status = 502
    default_message = "大模型调用失败"


class ParseFailed(AppError):
    code = "PARSE_FAILED"
    http_status = 422
    default_message = "解析结果校验失败"
