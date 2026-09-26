"""按角色最小披露。

商家机密与平台模型细节只在必要范围可见：
- 商家：仅看到与自己相关的规则要点；
- 审查人员：可复算影响，但商家机密以聚合形式披露，且无权修改生产规则；
- 审计人员：可见完整内容与事件日志。
"""
from __future__ import annotations

import copy
from collections import Counter

from .service import FairChangeService

# 平台模型细节：任何外部角色都不可见
PLATFORM_INTERNAL_FIELDS = ("algorithm_detail", "model_params", "internal_notes")
# 商家机密：仅审计可见，审查只见聚合
MERCHANT_SECRET_FIELDS = ("margin_floor", "cost_structure")
# 小于该样本量的聚合直接隐匿，防止反推单个商家
K_ANONYMITY = 5


def _scrub(node, fields) -> None:
    if isinstance(node, dict):
        for key in fields:
            node.pop(key, None)
        for value in node.values():
            _scrub(value, fields)
    elif isinstance(node, list):
        for item in node:
            _scrub(item, fields)


def disclose_package(pkg_view: dict, role: str,
                     viewer_merchant_id: str | None = None) -> dict:
    content = copy.deepcopy(pkg_view["content"])
    base = {
        "package_id": pkg_view["package_id"],
        "title": pkg_view["title"],
        "status": pkg_view["status"],
        "content_hash": pkg_view["content_hash"],
        "algorithm_version": content.get("algorithm_version"),
    }
    if role == "auditor":
        base["content"] = content
        base["disclosure"] = "完整内容（含平台模型细节与商家机密），仅限审计"
        return base
    _scrub(content, PLATFORM_INTERNAL_FIELDS)
    if role == "reviewer":
        _scrub(content, MERCHANT_SECRET_FIELDS)
        scope = content.get("merchant_scope", {})
        ids = scope.get("merchant_ids")
        if ids is not None:
            scope["merchant_ids"] = (
                {"count": len(ids)} if len(ids) >= K_ANONYMITY else "已隐匿（样本过小）"
            )
        base["content"] = content
        base["disclosure"] = "审查视图：可复算影响，商家机密以聚合形式披露"
        return base
    if role == "merchant":
        _scrub(content, MERCHANT_SECRET_FIELDS)
        scope = content.get("merchant_scope", {})
        ids = scope.get("merchant_ids", [])
        groups = scope.get("groups", [])
        base["content"] = {
            "traffic_entries": content.get("traffic_entries"),
            "subsidy_conditions": content.get("subsidy_conditions"),
            "price_constraints": content.get("price_constraints"),
            "appeal_policy": content.get("appeal_policy"),
            "applies_to_you": (
                viewer_merchant_id is not None
                and (viewer_merchant_id in ids or (not ids and not groups))
            ),
        }
        base["disclosure"] = "商家视图：仅披露与其相关的规则要点"
        return base
    raise ValueError(f"未知角色: {role}")


def reviewer_recompute(service: FairChangeService, package_id: str) -> dict:
    """审查复算视图：只给聚合数据，不给商家级机密，也不能改规则。"""
    view = service.package_view(package_id)
    exposures = [e for e in service.exposures() if e["package_id"] == package_id]
    metrics = [m for m in service.metrics()
               if service.attribute_metric(m) == package_id]
    demotions = Counter(
        m.get("group", "未知群体") for m in metrics if m.get("kind") == "demotion"
    )
    return {
        "package_id": package_id,
        "content_hash": view["content_hash"],
        "status": view["status"],
        "exposure_count": len(exposures),
        "metric_count": len(metrics),
        "demotions_by_group": {
            g: (n if n >= K_ANONYMITY else "已隐匿（样本过小）")
            for g, n in demotions.items()
        },
        "note": "审查人员可据此复算影响，但无权修改生产规则",
    }
