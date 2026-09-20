"""访问令牌与角色。

角色说明：
- admin       县林改办管理员（全部权限，敏感信息明文）
- clerk       经办人（登记、变更、签发、撤回，敏感信息明文）
- reviewer    复核员（复核队列与决定、审计查看，敏感信息明文）
- village     村集体（查看本账信息，证件号与电话脱敏）
- institution 授权机构（仅证照核验接口，持证人姓名脱敏）

默认令牌仅用于本地与验收环境；生产部署时在数据目录放置 tokens.json 覆盖，
格式：{"<token>": {"actor_id": "...", "role": "..."}}。
"""

from __future__ import annotations

import json
import os

DEFAULT_TOKENS = {
    "token-admin": {"actor_id": "admin", "role": "admin"},
    "token-clerk-a": {"actor_id": "clerk-a", "role": "clerk"},
    "token-clerk-b": {"actor_id": "clerk-b", "role": "clerk"},
    "token-reviewer": {"actor_id": "reviewer-1", "role": "reviewer"},
    "token-village": {"actor_id": "village-1", "role": "village"},
    "token-bank": {"actor_id": "bank-1", "role": "institution"},
}

ROLES = {"admin", "clerk", "reviewer", "village", "institution"}


def load_tokens(data_dir: str | None):
    tokens = {token: dict(identity) for token, identity in DEFAULT_TOKENS.items()}
    path = os.path.join(data_dir, "tokens.json") if data_dir else None
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            custom = json.load(fh)
        for token, identity in custom.items():
            tokens[token] = {"actor_id": identity["actor_id"], "role": identity["role"]}
    return tokens


class Authenticator:
    """按 Bearer 令牌识别经办身份。"""

    def __init__(self, tokens):
        self._tokens = tokens

    def authenticate(self, header: str | None):
        """返回 {"actor_id", "role"}；无令牌或令牌未知时返回 None。"""
        if not header or not header.startswith("Bearer "):
            return None
        token = header[len("Bearer "):].strip()
        identity = self._tokens.get(token)
        return dict(identity) if identity else None
