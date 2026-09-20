"""服务统一错误类型：所有阻断都携带结构化理由，便于办事人员与验收核对。"""

from __future__ import annotations


class ApiError(Exception):
    """带 HTTP 状态码与业务错误码的异常。"""

    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message, *, code=None, status=None, details=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.details = details or {}


class ValidationError(ApiError):
    """请求内容不合法（缺字段、依据不足、期限颠倒等）。"""

    status = 400
    code = "VALIDATION_ERROR"


class Unauthorized(ApiError):
    """缺少或未知的访问令牌。"""

    status = 401
    code = "UNAUTHENTICATED"


class Forbidden(ApiError):
    """角色无权访问该接口。"""

    status = 403
    code = "FORBIDDEN"


class NotFound(ApiError):
    """目标资源不存在。"""

    status = 404
    code = "NOT_FOUND"


class Conflict(ApiError):
    """与当前账本状态冲突（版本已被他人更新、重复复核、重复撤回等）。"""

    status = 409
    code = "CONFLICT"
