"""平台规则公平变更 —— 领域模型。

所有可发布对象均为不可变值对象；"冻结"通过两项机制保证：

1. ``dataclass(frozen=True)`` 使发布包及其组成部分构造后不可改写；
2. 每个发布包按规范化 JSON 计算 ``content_hash``，审批、灰度归属、曝光留痕
   全部引用该哈希。发布包任何一处被改动都会得到不同哈希，旧审批自动失效。

字段敏感度分级（``SENSITIVITY``）用于最小范围披露：
- PUBLIC            对外可见（规则编号、入口名称等）；
- PLATFORM_MODEL    平台模型细节，仅向审查人员在复算包中披露并留痕；
- MERCHANT_SECRET   商家机密，默认假名化，任何视图都不泄露给其他商家。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from typing import Any, Iterable

PUBLIC = "public"
PLATFORM_MODEL = "platform_model"
MERCHANT_SECRET = "merchant_secret"

# 字段名 -> 敏感度，披露视图据此脱敏
SENSITIVITY: dict[str, str] = {
    "ranking_weights": PLATFORM_MODEL,
    "internal_params": PLATFORM_MODEL,
    "cost_price": MERCHANT_SECRET,
    "merchant_ids": MERCHANT_SECRET,
    "settlement_data": MERCHANT_SECRET,
}


def canonical(obj: Any) -> str:
    """规范化 JSON 序列化：键排序、无空白，保证跨进程哈希一致。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    digest = hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


@dataclass(frozen=True)
class AlgorithmSpec:
    """算法版本与排序因子。完整模型参数以 ``params_artifact`` 留档，
    发布包内只保留复算所需的权重与参数指纹。"""

    version: str
    ranking_weights: dict[str, float] = field(default_factory=dict)
    factors: tuple[str, ...] = ()
    params_artifact: str = ""  # 外部工件存储地址/编号，不在包内展开
    internal_params: dict[str, float] = field(default_factory=dict)

    def score(self, merchant: dict[str, Any]) -> float:
        """按声明权重对商家因子做线性复算。因子缺失按 0 处理，
        使审查人员仅凭发布包即可重放排序结果。"""
        total = 0.0
        for name, weight in self.ranking_weights.items():
            total += weight * float(merchant.get("factors", {}).get(name, 0.0))
        return total


@dataclass(frozen=True)
class MerchantScope:
    """适用商家：分层命中 + 显式名单 - 排除名单。"""

    segments: tuple[str, ...] = ()
    merchant_ids: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def matches(self, merchant: dict[str, Any]) -> bool:
        mid = merchant["merchant_id"]
        if mid in self.exclude:
            return False
        if mid in self.merchant_ids:
            return True
        return merchant.get("segment") in self.segments


@dataclass(frozen=True)
class TrafficEntry:
    entry_id: str
    name: str
    # 该入口内分配给本规则覆盖流量的比例（0~1）
    traffic_share: float = 1.0


@dataclass(frozen=True)
class SubsidyCondition:
    """补贴条件。``withdrawn_when_violated`` 表示违反价格约束即取消补贴，
    是"被迫跟价"的组合成因之一。"""

    condition_id: str
    description: str
    applies_segments: tuple[str, ...]
    max_price_ratio: float  # 售价不得高于类目基准价的该比例，否则无补贴
    subsidy_rate: float
    withdrawn_when_violated: bool = True


@dataclass(frozen=True)
class PriceConstraint:
    """价格约束。

    kind:
      - floor_price     最低限价（value 为相对基准价比例）
      - follow_lowest   跟价：在 follow_depth 个名次内必须追随最低价
      - margin_cap      毛利上限
    enforcement: demotion（降权）/ delist（下架）/ fee（扣保证金）
    """

    constraint_id: str
    kind: str
    value: float
    enforcement: str
    follow_depth: int = 0
    applies_segments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExitTerms:
    """商家退出成本：锁定期与违约金比例。相对旧包上升即可能造成退出困难。"""

    lock_in_days: int = 0
    penalty_ratio: float = 0.0


@dataclass(frozen=True)
class NotificationRule:
    channel: str
    lead_days: int  # 生效前必须提前通知的天数
    audiences: tuple[str, ...]  # 受众分层
    template_ref: str = ""


@dataclass(frozen=True)
class AppealRule:
    window_days: int  # 申诉窗口
    freeze_pending: bool  # 申诉待裁期间是否冻结执行（降权/扣款暂缓）


@dataclass(frozen=True)
class RulePackage:
    """规则发布包：一次变更涉及的全部维度必须整体冻结。

    覆盖：算法版本、适用商家、流量入口、补贴条件、价格约束、
    通知规则、申诉规则，以及退出成本。
    """

    package_id: str
    revision: int
    algorithm: AlgorithmSpec
    scope: MerchantScope
    entries: tuple[TrafficEntry, ...]
    subsidies: tuple[SubsidyCondition, ...]
    price_constraints: tuple[PriceConstraint, ...]
    exit_terms: ExitTerms
    notification: NotificationRule
    appeal: AppealRule
    notes: str = ""
    created_by: str = ""

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    def entry_ids(self) -> set[str]:
        return {e.entry_id for e in self.entries}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RulePackage":
        """从冻结快照（普通 JSON 结构）原样重建发布包。"""
        d = dict(data)
        d["algorithm"] = AlgorithmSpec(**d["algorithm"])
        d["scope"] = MerchantScope(**d["scope"])
        d["entries"] = tuple(TrafficEntry(**e) for e in d["entries"])
        d["subsidies"] = tuple(SubsidyCondition(**s) for s in d["subsidies"])
        d["price_constraints"] = tuple(
            PriceConstraint(**c) for c in d["price_constraints"]
        )
        d["exit_terms"] = ExitTerms(**d["exit_terms"])
        d["notification"] = NotificationRule(**d["notification"])
        d["appeal"] = AppealRule(**d["appeal"])
        return cls(**d)


def to_jsonable(obj: Any) -> Any:
    """把值对象（含 dataclass）递归转为可 JSON 化的普通结构。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def iter_sensitive_paths(obj: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """遍历结构，产出 (点分路径, 敏感度)，供披露视图脱敏。"""
    if hasattr(obj, "__dataclass_fields__"):
        for f in fields(obj):
            child = f"{path}.{f.name}" if path else f.name
            level = SENSITIVITY.get(f.name)
            if level:
                yield child, level
            yield from iter_sensitive_paths(getattr(obj, f.name), child)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}" if path else k
            level = SENSITIVITY.get(k)
            if level:
                yield child, level
            yield from iter_sensitive_paths(v, child)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from iter_sensitive_paths(v, f"{path}[{i}]")
