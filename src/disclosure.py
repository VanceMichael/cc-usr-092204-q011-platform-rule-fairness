"""最小范围披露。

同一份冻结事实，按查看者身份给出不同视图：

- ``AUDITOR`` 审查人员：可见平台模型细节以便复算；其他商家身份假名化，
  看不到商家机密；对发布包只有只读权限（写权限在 API 层拒绝）。
- ``MERCHANT`` 商家：只能看到与自身相关的规则与自己的影响行，看不到
  模型内部参数，也看不到其他商家。
- ``OPERATOR`` 运营：可见运营所需全部字段，但查看他商家机密同样留痕。
- ``PUBLIC``：仅公开维度。

假名化用稳定哈希前缀：同一商家在同一审查会话中保持一致假名，可做群体
分析但无法还原身份。
"""

from __future__ import annotations

import hashlib
from typing import Any

from .models import MERCHANT_SECRET, PLATFORM_MODEL, to_jsonable

OPERATOR = "OPERATOR"
AUDITOR = "AUDITOR"
MERCHANT = "MERCHANT"
PUBLIC = "PUBLIC"

REDACTED_MODEL = "[REDACTED:platform_model]"
REDACTED_SECRET = "[REDACTED:merchant_secret]"


def pseudonym(merchant_id: str) -> str:
    return "M-" + hashlib.sha256(merchant_id.encode()).hexdigest()[:8]


# 各角色可见的最高敏感度
_VISIBLE = {
    PUBLIC: frozenset(),
    MERCHANT: frozenset(),
    AUDITOR: frozenset({PLATFORM_MODEL}),
    OPERATOR: frozenset({PLATFORM_MODEL, MERCHANT_SECRET}),
}


class DisclosureView:
    def __init__(self, store, role: str, viewer: str = "",
                 self_merchant_id: str | None = None) -> None:
        self.store = store
        self.role = role
        self.viewer = viewer
        self.self_merchant_id = self_merchant_id

    def _can_see(self, level: str) -> bool:
        return level in _VISIBLE.get(self.role, frozenset())

    # ---- 发布包脱敏 ----------------------------------------------------
    def package_view(self, package_hash: str) -> dict[str, Any]:
        pkg = self.store.package(package_hash)
        data = to_jsonable(pkg)
        data["content_hash"] = package_hash
        return self._redact_obj(data)

    def _redact_obj(self, obj: Any, field_name: str = "") -> Any:
        from .models import SENSITIVITY
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                level = SENSITIVITY.get(k)
                if level == PLATFORM_MODEL and not self._can_see(PLATFORM_MODEL):
                    out[k] = REDACTED_MODEL
                elif level == MERCHANT_SECRET and \
                        not self._can_see(MERCHANT_SECRET):
                    out[k] = self._redact_secret(k, v)
                else:
                    out[k] = self._redact_obj(v, k)
            return out
        if isinstance(obj, list):
            return [self._redact_obj(v, field_name) for v in obj]
        return obj

    def _redact_secret(self, key: str, value: Any) -> Any:
        # 商家视图：显式名单里只保留"自己是否在名单内"，其余商家假名/隐藏
        if key == "merchant_ids" and isinstance(value, list):
            if self.role == MERCHANT:
                return {"includes_self": self.self_merchant_id in value}
            return [pseudonym(v) for v in value]
        return REDACTED_SECRET

    # ---- 影响复算脱敏 --------------------------------------------------
    def impact_view(self, report: dict[str, Any]) -> dict[str, Any]:
        can_secret = self._can_see(MERCHANT_SECRET)
        can_model = self._can_see(PLATFORM_MODEL)
        out = dict(report)
        if not can_model:
            out["weight_change"] = REDACTED_MODEL
        rows = []
        for r in report.get("rows", []):
            if self.role == MERCHANT and \
                    r["merchant_id"] != self.self_merchant_id:
                continue
            nr = dict(r)
            if not can_secret:
                nr["merchant_id"] = (
                    nr["merchant_id"] if self.role == MERCHANT
                    else pseudonym(nr["merchant_id"]))
            rows.append(nr)
        out["rows"] = rows
        return out

    # ---- 历史/曝光脱敏 -------------------------------------------------
    def redact_merchant(self, merchant_id: str) -> str:
        if self.role == MERCHANT:
            return merchant_id if merchant_id == self.self_merchant_id \
                else pseudonym(merchant_id)
        if self._can_see(MERCHANT_SECRET):
            return merchant_id
        return pseudonym(merchant_id)
