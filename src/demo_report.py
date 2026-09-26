"""端到端演示：跑完"候选规则 -> 上线闸门 -> 灰度曝光 -> 组合预警 ->
投诉反查 -> 紧急止损/回滚"全流程并打印中文报告。

运行：python3 -m src.demo_report
"""

from __future__ import annotations

from src import alerts as alerts_mod
from src import impact
from src import trace as trace_mod
from src.demo import ENTRY_SEARCH, Clock, build_demo_store
from src.store import REQUIRED_TEAMS


def main() -> None:
    clock = Clock()
    store, clock, bh, ch = build_demo_store(clock)
    print("=" * 68)
    print("平台规则公平变更后台 —— 端到端演示（合成数据）")
    print("=" * 68)

    # 1. 上线前清单
    pre = store.preflight(ch)
    print("\n[1] 上线前清单")
    print(f"  受影响分层：{pre['affected_segments']}")
    print(f"  受影响商家：{pre['affected_merchant_count']} 家")
    print(f"  缺会签团队：{pre['missing_approval_teams']}")
    print(f"  未完成通知：{len(pre['outstanding_notifications'])} 家"
          f"（含提前期不足）  ready={pre['ready']}")

    # 2. 会签 + 通知 + 灰度
    for team in REQUIRED_TEAMS:
        store.approve(f"approver-{team}", team, ch)
    for mid in store.affected_merchants(ch):
        store.record_notification("ops", ch, mid, ts=clock.t - 9 * 86400)
    print("\n[2] 四团队会签完成、提前 9 天通知完毕")
    print(f"  preflight.ready = {store.preflight(ch)['ready']}")
    store.start_gray("ops", ch, 50)
    print("  候选包进入 50% 灰度")

    # 3. 灰度期间逐次曝光归属
    print("\n[3] 灰度曝光归属（每次曝光都对应生效规则）")
    for mid in ("m-a1", "m-a2", "m-a3", "m-a4"):
        rec = store.resolve_exposure("serving", mid, ENTRY_SEARCH)
        print(f"  {mid} -> layer={rec['layer']:<10} "
              f"algo={rec['algorithm_version']} hash={rec['content_hash']}")

    # 4. 影响复算
    report = impact.compare(store, ch, bh)
    print("\n[4] 影响复算（审查人员可独立重放）")
    for r in report["rows"]:
        if r["segment"] == "A":
            print(f"  {r['merchant_id']}: 名次 {r['old_rank']}->{r['new_rank']}"
                  f"（{'降权' if r['demoted'] else '未降权'}），"
                  f"跟价缺口={r['follow_gap']}，"
                  f"锁定期变化=+{r['lock_in_delta_days']}天")

    # 5. 组合预警
    found = alerts_mod.evaluate(store, ch, bh, record_actor="monitor")
    print("\n[5] 规则组合可解释预警")
    for a in found:
        print(f"  [{a['severity'].upper()}] {a['code']} @ 分层{a['segment']}")
        print(f"    指标：{a['metric']}")
        print(f"    解释：{a['explanation']}")

    # 6. 投诉反查
    rec = store.resolve_exposure("serving", "m-a3", ENTRY_SEARCH)
    traced = trace_mod.trace_complaint(store, "m-a3")
    chain = next(c for c in traced["rule_chains"]
                 if c["content_hash"] == rec["content_hash"])
    print("\n[6] 从商家 m-a3 投诉反查")
    print(f"  曝光数：{traced['exposure_count']}")
    print(f"  命中规则：{chain['package_id']} r{chain['revision']} "
          f"算法 {chain['algorithm_version']}")
    print("  批准依据：")
    for ap in chain["approval_basis"]:
        print(f"    - {ap['team']} / {ap['approver']} @ {ap['ts']:.0f}")

    # 7. 紧急止损与回滚不抹历史
    print("\n[7] 紧急止损 + 回滚（只增不改）")
    store.emergency_stop("oncall", ch, "投诉与降权指标异常集中")
    store.rollback("oncall", bh, "恢复基线排序")
    kinds = [e.kind for e in store.history(ch)]
    print(f"  候选包事件轨迹：{kinds}")
    print(f"  当前正式版本：{store.production_hash}（基线 r1）")

    # 8. 账本校验
    ok, msg = store.ledger.verify()
    print(f"\n[8] 哈希链账本校验：intact={ok}（{msg}），"
          f"共 {len(store.ledger.events())} 条事件")


if __name__ == "__main__":
    main()
