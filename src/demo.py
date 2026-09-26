"""去标识演示场景：构造一个会同时触发集中降权、被迫跟价、退出困难
三类组合预警的规则变更，供测试与本地演示复用。

分层 A 有 4 家商家，因子与价格如下（均为合成数据）：

| 商家 | 质量因子 q | 价格因子 p | 售价 | 类目基准价 |
|------|-----------|-----------|------|-----------|
| m-a1 | 1 | 4 | 10 | 11 |
| m-a2 | 2 | 3 | 12 | 11 |
| m-a3 | 3 | 2 | 13 | 11 |
| m-a4 | 4 | 1 | 14 | 11 |

- 基线算法按 q 排序，新算法改按 p 排序 -> a3/a4 名次下滑；
- 新规则要求追随最低价 a1，售价高于补贴上限即撤补贴 -> a2/a3/a4 双向受压；
- 新规则把锁定期从 0 提到 90 天并加收违约金 -> 同一批商家退出困难。
"""

from __future__ import annotations

from .models import (
    AlgorithmSpec, AppealRule, MerchantScope, NotificationRule,
    PriceConstraint, RulePackage, SubsidyCondition, TrafficEntry, ExitTerms,
)
from .store import REQUIRED_TEAMS, RuleStore

ENTRY_SEARCH = "search_home"

MERCHANTS = [
    {"merchant_id": "m-a1", "segment": "A", "price": 10,
     "category_ref_price": 11, "factors": {"q": 1, "p": 4}},
    {"merchant_id": "m-a2", "segment": "A", "price": 12,
     "category_ref_price": 11, "factors": {"q": 2, "p": 3}},
    {"merchant_id": "m-a3", "segment": "A", "price": 13,
     "category_ref_price": 11, "factors": {"q": 3, "p": 2}},
    {"merchant_id": "m-a4", "segment": "A", "price": 14,
     "category_ref_price": 11, "factors": {"q": 4, "p": 1}},
    {"merchant_id": "m-b1", "segment": "B", "price": 20,
     "category_ref_price": 20, "factors": {"q": 2, "p": 2}},
    {"merchant_id": "m-b2", "segment": "B", "price": 21,
     "category_ref_price": 20, "factors": {"q": 3, "p": 1}},
]


def baseline_package() -> RulePackage:
    return RulePackage(
        package_id="RULE-SEARCH", revision=1,
        algorithm=AlgorithmSpec(version="algo-v1",
                                ranking_weights={"q": 1.0},
                                factors=("q",)),
        scope=MerchantScope(segments=("A", "B")),
        entries=(TrafficEntry(ENTRY_SEARCH, "首页搜索", 1.0),),
        subsidies=(),
        price_constraints=(),
        exit_terms=ExitTerms(0, 0.0),
        notification=NotificationRule("in_app", 7, ("A", "B")),
        appeal=AppealRule(30, True),
        notes="基线规则：质量因子排序，无限价无锁定",
        created_by="ops-demo",
    )


def candidate_package() -> RulePackage:
    return RulePackage(
        package_id="RULE-SEARCH", revision=2,
        algorithm=AlgorithmSpec(version="algo-v2",
                                ranking_weights={"p": 1.0},
                                factors=("p",)),
        scope=MerchantScope(segments=("A", "B")),
        entries=(TrafficEntry(ENTRY_SEARCH, "首页搜索", 1.0),),
        subsidies=(SubsidyCondition(
            "SUB-LOWPRICE", "低价补贴", ("A",),
            max_price_ratio=1.0, subsidy_rate=0.08,
            withdrawn_when_violated=True),),
        price_constraints=(PriceConstraint(
            "PC-FOLLOW", "follow_lowest", value=0.0,
            enforcement="demotion", applies_segments=("A",)),),
        exit_terms=ExitTerms(90, 0.10),
        notification=NotificationRule("in_app", 7, ("A", "B")),
        appeal=AppealRule(30, True),
        notes="改为价格因子排序、要求跟价、违价撤补贴、新增90天锁定",
        created_by="ops-demo",
    )


class Clock:
    """确定性可控时钟，默认每次读取递增 1 秒。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        self.t += 1
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def build_demo_store(clock: Clock | None = None,
                     *, candidate: bool = True) -> tuple[RuleStore, Clock,
                                                         str, str | None]:
    """返回 (store, clock, 基线包哈希, 候选包哈希)。

    基线包已会签、通知、全量上线并标记为回滚基线；候选包处于 DRAFT。
    """
    clock = clock or Clock()
    store = RuleStore(clock=clock)
    for m in MERCHANTS:
        store.register_merchant("ops-demo", m)

    base = baseline_package()
    bh = store.create_package("ops-demo", base)
    for team in REQUIRED_TEAMS:
        store.approve(f"approver-{team}", team, bh)
    # 通知提前 9 天发出，满足 7 天提前期
    notified_ts = clock.t - 9 * 86400
    for mid in store.affected_merchants(bh):
        store.record_notification("ops-demo", bh, mid, ts=notified_ts)
    store.activate("ops-demo", bh)
    store.mark_baseline("ops-demo", bh)

    ch = None
    if candidate:
        ch = store.create_package("ops-demo", candidate_package())
    return store, clock, bh, ch
