"""平台规则公平变更领域模型与常量。

发布包把算法版本、适用商家、流量入口、补贴条件、价格约束、
通知计划与申诉政策冻结成不可变内容；冻结之后只能追加事件，
任何止损、回滚、豁免都不得改写历史。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# 发布包冻结时必须齐备的内容字段
PACKAGE_CONTENT_FIELDS = (
    "algorithm_version",   # 算法版本
    "merchant_scope",      # 适用商家
    "traffic_entries",     # 流量入口
    "subsidy_conditions",  # 补贴条件
    "price_constraints",   # 价格约束
    "notification_plan",   # 通知计划
    "appeal_policy",       # 申诉政策
    "rollout",             # 灰度计划
)

# 多团队会签：全部团队通过才允许发布
DEFAULT_REQUIRED_TEAMS = ("运营", "算法", "法务", "风控")

# 事件类型（事件日志只增不改）
EVENT_TYPES = (
    "package_drafted",
    "package_updated",
    "package_frozen",
    "approval_submitted",
    "notification_sent",
    "package_published",
    "package_promoted",
    "package_superseded",
    "package_rolled_back",
    "emergency_stopped",
    "exemption_granted",
    "alert_raised",
)

# 仍在线上生效的状态
ACTIVE_STATUSES = ("published", "official")


def canonical_hash(content: dict[str, Any]) -> str:
    """对发布包内容做规范化哈希，冻结后内容不可变、可校验。"""
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class RulePackage:
    """规则发布包。content_hash 一旦写入即视为冻结。"""

    package_id: str
    title: str
    content: dict[str, Any]
    created_by: str
    created_at: str
    content_hash: str | None = None

    def frozen(self) -> bool:
        return self.content_hash is not None
