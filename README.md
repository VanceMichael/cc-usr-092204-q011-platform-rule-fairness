# 平台规则公平变更后台

记录平台算法、流量、价格规则的发布、灰度、影响与追责。一次规则小改可能同时
改变搜索排序、流量分配、价格补贴与退出成本；本系统把这些维度**整体冻结为
发布包**，并以只增哈希链账本保证"低价内耗究竟来自商家选择还是平台机制"
可被外部判定与复算。

## 领域事实

- 监管将加强平台数据、算法、流量和规则治理
- 平台企业承担守门人责任
- 整治目标包括低价内耗与不正当竞争

## 核心能力（对应治理要求）

| 要求 | 实现 |
|------|------|
| 六个维度冻结成发布包 | `models.py`：算法版本/适用商家/流量入口/补贴条件/价格约束/通知与申诉（另含退出成本）构成不可变 `RulePackage`，按规范化 JSON 计算 `content_hash`；改动任一字段即得到新哈希，旧审批失效 |
| 灰度期间每次曝光对应生效规则 | `store.resolve_exposure`：按 适用商家×入口×队列×确定性分桶 解析到唯一发布包，记录算法版本、权重、实际/暂缓执行的约束、豁免与冻结申诉，并落 `EXPOSURE_RESOLVED` 事件 |
| 紧急止损/回滚/豁免不抹历史 | 全部为只增事件：`EMERGENCY_STOP`、`ROLLBACK`、`EXEMPTION_GRANTED/REVOKED`；回滚是追加事件并重新激活旧包，被止损记录原样保留 |
| 规则组合异常产生可解释预警 | `alerts.py`：集中降权 `CONCENTRATED_DEMOTION`、被迫跟价 `FORCED_PRICE_FOLLOWING`、退出困难 `EXIT_DIFFICULTY`，每条给出规则组件编号、指标/阈值、受影响商家与自然语言解释 |
| 最小范围披露 | `disclosure.py`：审查人可见模型权重以便复算、商家身份假名化；商家只见本人；模型细节与商家机密分级脱敏 |
| 审查可复算但不能改生产规则 | `api.py`：AUDITOR 仅有只读+复算+反查+校验权限，任何建包/审批/放量/止损/回滚/豁免均返回 403 |
| 多团队同时审批只一个正式版本 | 四团队（规则治理/算法/合规/商家生态）会签，临界区加锁串行化；全平台同时刻只允许一个 PRODUCTION 与一条灰度 |
| 上线前列出受影响群体与未完成通知 | `preflight`：受影响分层/商家、缺会签团队、未通知或提前期不足清单；闸门不过禁止灰度/上线 |
| 上线后从投诉/竞争指标反查 | `trace.py`：投诉→曝光→发布包→规则组件→批准依据（审批人/时间/意见）→豁免/申诉/止损→预警；竞争指标→生效包时间线与组合预警 |
| 申诉待裁冻结执行 | `freeze_pending=True` 时，待裁申诉使相关价格约束的降权/扣款暂缓 |

## 目录

- `src/models.py` — 冻结发布包值对象、内容哈希、敏感度分级
- `src/ledger.py` — 只增哈希链事件账本（可校验、可重放、防篡改）
- `src/store.py` — 规则中枢：审批、闸门、灰度、曝光归属、止损/回滚/豁免/申诉、快照
- `src/impact.py` — 确定性影响复算（排序、跟价压力、补贴流失、退出成本）
- `src/alerts.py` — 规则组合可解释预警
- `src/disclosure.py` — 最小范围披露与假名化
- `src/trace.py` — 投诉/竞争指标反查证据链
- `src/api.py` — 角色权限应用服务
- `src/server.py` — HTTP 入口（标准库，零三方依赖）
- `src/demo.py` / `src/demo_report.py` — 去标识合成场景与端到端演示
- `contracts/` — 发布包与领域上下文 JSON 契约
- `fixtures/` — 去标识样例
- `tests/` — 33 个单元/集成测试

## 本地检查

```bash
python3 -m unittest discover -s tests -v   # 全量测试
python3 -m src.demo_report                 # 端到端中文演示
python3 -m src.server                      # 启动 HTTP 服务（127.0.0.1:8000）
```

设置 `RULE_STATE_FILE=/path/state.json` 后，每次写操作把含哈希链的快照
原子落盘，重启自动恢复并校验。

## HTTP 接口（角色通过请求头声明）

头：`X-Role: OPERATOR|APPROVER|AUDITOR|MERCHANT`、`X-User`、
`X-Team`（审批团队代号 `governance|algorithm|compliance|ecosystem`）、
`X-Merchant-Id`。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/merchants` | 录入商家（复算输入） |
| POST | `/packages` | 创建并冻结发布包，返回 content_hash |
| GET  | `/packages` / `/packages/<hash>` | 列表 / 脱敏详情 |
| POST | `/packages/<hash>/approve` | 团队会签 |
| GET  | `/packages/<hash>/preflight` | 上线前：受影响群体+未完成通知+会签缺口 |
| POST | `/packages/<hash>/notify` | 记录商家通知（含时间，校验提前期） |
| POST | `/packages/<hash>/gray` | 开始灰度（percent、cohorts） |
| POST | `/gray/adjust` | 调整灰度比例 |
| POST | `/packages/<hash>/activate` | 全量上线（成为唯一正式版本） |
| POST | `/packages/<hash>/stop` | 紧急止损（留痕） |
| POST | `/rollback` | 回滚到历史包（追加事件） |
| POST | `/packages/<hash>/exemptions` | 授予豁免 |
| POST | `/exemptions/<id>/revoke` | 撤销豁免（留痕） |
| POST | `/exposures` | 生产侧逐次曝光规则归属 |
| POST | `/appeals` | 提交申诉（待裁冻结执行） |
| POST | `/appeals/<id>/resolve` | 裁决 |
| GET  | `/packages/<hash>/impact?base=<hash>` | 审查只读影响复算 |
| GET  | `/packages/<hash>/alerts?base=<hash>&record=1` | 组合预警（运营可落账） |
| GET  | `/packages/<hash>/history` | 完整事件轨迹 |
| GET  | `/trace/complaint?merchant_id=...` | 从投诉反查规则与批准依据 |
| GET  | `/trace/metric?segment=...` | 从竞争指标反查生效包与预警 |
| GET  | `/ledger/verify` | 哈希链完整性校验 |
