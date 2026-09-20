"""角色鉴权与按角色脱敏。

服务使用不记名令牌（Bearer Token）标识经办人身份。令牌与角色的映射可以通过
环境变量 ``FOREST_TOKENS`` 覆盖（JSON 对象，键为令牌，值为 ``{"id","role","name"}``）。
未配置时使用仅适合本地开发的默认令牌。
"""

from __future__ import annotations

import json
import os
from typing import Any

# 角色常量
OPERATOR = "operator"        # 经办人：登记宗地、提交变更、补录确认
REVIEWER = "reviewer"        # 复核员：审批/驳回变更
REGISTRAR = "registrar"      # 登记发证机关：签发、撤回证照
INSTITUTION = "institution"  # 授权机构（如银行）：核验证照

ROLES = {OPERATOR, REVIEWER, REGISTRAR, INSTITUTION}

# 能够看到完整敏感身份信息的内部角色
INTERNAL_ROLES = {OPERATOR, REVIEWER, REGISTRAR}

_DEFAULT_TOKENS: dict[str, dict[str, str]] = {
    "tok-operator": {"id": "op-zhang", "role": OPERATOR, "name": "张经办"},
    "tok-operator-2": {"id": "op-li", "role": OPERATOR, "name": "李经办"},
    "tok-reviewer": {"id": "rv-wang", "role": REVIEWER, "name": "王复核"},
    "tok-registrar": {"id": "rg-li", "role": REGISTRAR, "name": "李登记"},
    "tok-institution": {"id": "inst-bank", "role": INSTITUTION, "name": "青山绿金银行"},
}

# 递归脱敏时需要遮盖的字段名
_SENSITIVE_KEYS = {"id_card", "phone"}


def load_tokens() -> dict[str, dict[str, str]]:
    """从环境变量加载令牌映射，缺省时返回开发用默认令牌。"""

    raw = os.environ.get("FOREST_TOKENS")
    if not raw:
        return dict(_DEFAULT_TOKENS)
    parsed = json.loads(raw)
    for principal in parsed.values():
        if principal.get("role") not in ROLES:
            raise ValueError(f"未知角色：{principal.get('role')}")
    return parsed


def principal_from_headers(
    tokens: dict[str, dict[str, str]], headers: Any
) -> dict[str, str] | None:
    """从 HTTP 头解析身份，未携带或令牌无效时返回 None。"""

    authorization = headers.get("Authorization") or headers.get("authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    principal = tokens.get(token)
    return dict(principal) if principal else None


def _mask_id_card(value: str) -> str:
    if len(value) < 10:
        return "*" * len(value)
    return value[:6] + "*" * (len(value) - 10) + value[-4:]


def _mask_phone(value: str) -> str:
    if len(value) < 7:
        return "*" * len(value)
    return value[:3] + "****" + value[-4:]


def mask_for_role(data: Any, role: str | None) -> Any:
    """按角色递归遮盖敏感身份信息。

    内部角色可见原文；授权机构可见姓名，但身份证号与电话脱敏；
    其他情况（理论上不会通过鉴权）同样脱敏。
    """

    if role in INTERNAL_ROLES:
        return data
    return _mask(data)


def _mask(data: Any) -> Any:
    if isinstance(data, dict):
        masked = {}
        for key, value in data.items():
            if key in _SENSITIVE_KEYS and isinstance(value, str) and value:
                if key == "id_card":
                    masked[key] = _mask_id_card(value)
                else:
                    masked[key] = _mask_phone(value)
            else:
                masked[key] = _mask(value)
        return masked
    if isinstance(data, list):
        return [_mask(item) for item in data]
    return data
