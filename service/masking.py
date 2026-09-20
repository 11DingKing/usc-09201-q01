"""按角色脱敏：敏感身份信息只向职责范围内的角色开放。

- admin / clerk / reviewer：明文（经办与复核职责所需）；
- village：成员姓名可见，证件号与电话脱敏；
- institution：不开放成员清单，核验结果中的持证人姓名脱敏。
"""

from __future__ import annotations

import copy

FULL_ROLES = {"admin", "clerk", "reviewer"}


def mask_id_number(value: str) -> str:
    """证件号保留前 3 位与后 4 位，中间脱敏。"""
    value = value or ""
    if len(value) <= 7:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def mask_phone(value: str) -> str:
    """电话保留前 3 位与后 4 位。"""
    value = value or ""
    if len(value) < 7:
        return "*" * len(value)
    return value[:3] + "****" + value[-4:]


def mask_name(value: str) -> str:
    """姓名仅保留首字。"""
    value = value or ""
    if not value:
        return value
    return value[0] + "*" * (len(value) - 1)


def mask_view(view, role: str):
    """递归脱敏视图中的成员证件号与电话；机构角色额外脱敏成员姓名。"""
    if role in FULL_ROLES:
        return view
    view = copy.deepcopy(view)

    def walk(node):
        if isinstance(node, dict):
            if "id_number" in node:
                node["id_number"] = mask_id_number(str(node["id_number"]))
            if "phone" in node:
                node["phone"] = mask_phone(str(node["phone"]))
            if role == "institution" and "member_id" in node and "name" in node:
                node["name"] = mask_name(str(node["name"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(view)
    return view
