# M3 失败结果与冲突边界补齐

日期：2026-09-13 至 2026-09-14。状态：实现、独立代码审查与最终验证完成。

本轮先执行 `git fetch origin`，确认当前 HEAD 与 `origin/master` 同为
`d9d7092`，工作树无本地改动。213 项 unittest 和两份既有 M3 消融实验
通过。新反例说明原回归不足以证明全部冻结契约闭合，因此本轮继续补齐 M3，
M4 仍是 [roadmap](../roadmap.md) 中的后续工作。

## 当前证据与计划

1. **目标提交事实与 journal 发布事实混淆。** 在目标已完成替换后，最终
   `PlanRecord.write` 于发布前失败，`execute_execution_plan` 抛出的
   `ApplyError.committed` 为 `False`，目标实际已是 desired。反过来，journal
   自身发布成功也不能证明任何目标已经写入。执行器应从实际 writer 进度累计
   目标事实，所有 journal 失败出口都保留它；API/CLI 不能靠重读 journal
   才知道本次调用已经做过什么。
1. **恢复的汇总掩盖未执行资源。** 合法 journal 中唯一资源为
   `not-attempted` 时，恢复保留该资源状态，却返回顶层 `confirmed`。
   仅当所有资源的当前重观察均为 `confirmed`，顶层才可确认；未知和未执行
   资源继续要求人工处理。恢复仍然只读，不重放、不回滚。
1. **未选字段的冲突未阻止反向写回。** 同一资源中 `setting` 为
   `conflict/review`，`runtime` 为 `live_changed/reverse_candidate`，选择
   `runtime` 仍能反向写回并返回 `committed`。在 reverse-sync 构建补丁前
   拒绝资源级 `Status.CONFLICT`，包括由不支持的归属方向产生的 review。
   不把此 guard 放进 accept 共用入口；显式 baseline acceptance 仍可按用户
   选择接受 desired/live，保留其原有权限语义。

M3a 观察及原 M3b/M3c 功能继续按 [M3 冻结范围](m3.md) 验证，不扩大到
动态模板反向映射、provider、自动恢复或不合作写入者的强一致性保证。
没有生产反例和契约依据的候选不作为新增开发项。

## 待审接口与开发分工

执行失败沿用 `ApplyError`，增加可选的 metadata-only `execution` 上下文。
它只包含 `plan_id`、`committed`、`changed_targets` 和按计划排序的
`resources`（每项仅 `name`、`target`、`state`），由执行器固定构造，CLI
按允许字段序列化，不透传任意字典或异常载荷。删除草案中与 error/journal
重复的 `execution.state`。资源状态定义如下：

- `not-attempted`：尚未进入目标 writer；在 intent journal 失败时仍为此状态。
- `failed`：已经尝试 writer，但 writer 明确报告未替换目标。这是错误上下文
  的新增区分，不改变 journal 的四态资源结果或持久 schema。
- `unknown`：writer 报告已替换但后续持久性、清理或目标验证不能确认；
  不将未知结果升级为成功。
- `committed`：writer 与目标后置条件已成功确认；之后 journal 失败不降级此事实。
- `unchanged`：已完成当前 no-op 的处理。

`changed_targets` 按计划顺序列出已知发生目标替换的路径，包括 writer 报告
`committed=True` 后抛错的目标；未进入 writer 或明确未替换的目标不加入。
`ApplyError.committed == execution.committed == bool(changed_targets)`。
`PlanRecordError.committed` 继续只表示 journal 发布，与目标事实分离。
一个外层执行错误边界附加内存上下文，避免各个失败出口重复拼装。
CLI 失败增加对应 `execution` 对象，
journal 仍单独描述实际可读取的持久记录，不用内存状态伪装成已持久化状态。
此为 v5 错误输出的增量修正，v1–v4 输出及持久 journal schema 不变。

| 包                | 不重叠写集                                                                                                     | 验收                                                                                                                  |
| ----------------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| A：执行失败事实   | `errors.py` 的可选 execution 上下文；`reconcile.py` 的 execution/write-record/failure helper；独立执行失败测试 | 初始、逐资源、最终 journal 故障；N 成功/N+1 失败；journal 不可读时仍保留每个目标结果；不混淆 journal 与 target commit |
| B：授权与恢复汇总 | `mutations.py` 的 reverse-sync guard；`reconcile.py` 的 recovery 汇总；独立冲突和恢复测试                      | preview/确认均阻断未选字段 conflict/review；显式 accept 仍可使用；单独 not-attempted 不误报 confirmed；只读快照保持   |
| C：CLI 失败输出   | `cli.py`；独立 CLI 故障测试                                                                                    | JSON 与人读输出可见累计目标状态；journal 不可用也能续查；无值、摘要、异常载荷泄漏；旧版本兼容                         |
| 主 agent：整合    | 本计划、owning docs、快速消融实验、集成验证                                                                    | 汇总独立 review，冻结接口后派发 luna max；审核交界，完成全套回归和仓库 gate                                           |

A 与 B 在 `reconcile.py` 中仅修改各自函数，开发期间不整文件格式化。
C 按冻结的 `ApplyError.execution` 接口实现，A 完成后做真实集成测试。
不引入事务引擎、额外持久日志、通用 provider/状态框架或第二套 recovery。

故障矩阵覆盖初始 journal 发布前/后、plan/resource preflight、plan intent、
no-op 记录、resource intent、writer 替换前/后、目标后置条件、resource commit
记录、failure marking 自身失败及最终 plan commit 记录。分别断言真实目标、
内存上下文、实际可读 journal；全 no-op 的最终日志失败也必须保持
`committed=False`。恢复覆盖 standalone not-attempted API/CLI，授权覆盖
未选真实冲突、未选错误方向 review、合法 forward/reverse 并存及显式 accept。

## 审查与消融顺序

由不同 sol medium agent 从逻辑、授权/归属/语义、保密/文件边界/可验证性
审查本草案。主 agent 汇总采纳或拒绝理由，冻结必要契约。
随后用真实生产执行器、writer、恢复入口及临时目录运行短小探针：

- 去掉执行器累计事实：应重现最后一次 journal 失败时的假 `committed=False`；
  增加同一 journal 的内存异常上下文应足够，不需要第二份持久 ledger。
- 去掉“全部资源已确认”的汇总条件：应重现单独 not-attempted 的假确认；
  只读结果汇总即可修复，不需要恢复引擎。
- 去掉资源级 conflict guard：应重现未选字段冲突时仍反向写回；
  一个现有状态检查即可，不需要通用策略 registry。

消融证明方案取舍，生产故障注入回归证明最终实现。开发完成后再由独立
reviewer 检查实现，主 agent 逐项核验 API/CLI、零写入、部分成功、保密、
兼容、文档以及仓库 gate，未通过项不标完成。

## 主 agent 审查汇总

三个独立 sol medium agent 分别审查逻辑/可验证性、授权/归属/语义、保密/
文件边界。三项反例均有当前生产代码证据，纳入补齐；保留 M3a implemented，
本轮初始 M3b/M3c 为 partial。

采纳两组共同建议，删除重复且含义不清的 `execution.state`；采纳逻辑审查的
`failed` 区分，避免把明确未替换的失败写成 unknown 或未尝试。保留逐资源
target/state、changed_targets 和 plan_id，分别承担续查位置、部分结果、
已发生替换及日志关联。拒绝第二份持久 ledger、通用事务/策略 registry。

冲突检查复用现有 `Status.CONFLICT` 聚合语义；不再复制一套逐字段 review
规则。accept 是显式接受比较点的独立动作，不共用此新增阻断。
恢复仅收紧当前结果的汇总，不叠加顶层 journal 状态策略，也不加入自动重放。
NOOP 的非协作外部写入竞态属于冻结范围外，未作为本轮功能缺口。

## 消融结果

开发前运行 `uv run experiments/m3_execution_closure_ablation.py`：四个生产
操作探针通过。初始 journal 已发布但目标未写、最终 journal 未发布但目标已写
这两个相反场景，均证明 journal publication 无法替代累计目标事实。单独
not-attempted 和未选字段冲突分别在删除对应 guard 后重现假成功与未经当前
冲突审查的反向写入。最小方案没有第二持久 ledger、恢复引擎、策略 registry
或重复的 execution 总状态；原有 journal schema 保持不变。

此实验使用真实执行器和 writer 加短小候选 wrapper，仅证明设计取舍；完整
时序、失败标记故障、部分提交和 CLI 保密以随后生产实现回归为准。

实现阶段继续做等价收敛：删除仅包装三个局部变量的 `_ExecutionContext`
类型及其转发方法，复用一个外层错误边界构造固定元数据；合并 writer 返回
未提交与抛出明确未提交异常的相同失败处理。既有状态、归属聚合和 journal
继续承担各自职责，没有新增持久类型或事务抽象。

## 实现分工与终审

三个 luna max agent 按 A/B/C 写集完成执行器、授权/恢复和 CLI；模型容量
中断后，另一个 luna max agent 接续补齐测试矩阵及类型修正。主 agent 负责
交界审查、文档、消融和最终集成。

独立 sol medium 终审确认三项生产修复逻辑成立，保密/文件边界没有剩余
阻断。终审要求补齐 plan/resource preflight journal 的直接故障注入，并在
最终 gate 后统一状态文档；这些验收项不以先前的绿测替代。

## 完成审计

| 要求                    | 当前证据                                                                                                               | 结论        |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------- | ----------- |
| 先同步最新基线          | `git fetch origin` 后 HEAD 与 `origin/master` 均为 `d9d7092`，差异 0，无需快进                                         | verified    |
| M3a 冻结观察契约        | manifest、ownership、严格 JSON 与只读观察的既有回归继续通过                                                            | implemented |
| M3b 冲突审查边界        | `test_m3_conflict_recovery.py` 覆盖未选冲突/错误方向、preview/confirm 零写、合法双向候选及显式 accept                  | implemented |
| M3c 目标提交事实        | `test_m3c_execution_outcomes.py` 覆盖完整 journal 阶段、writer 前后、后置条件、failure marking、raw cleanup 和全 no-op | implemented |
| M3c 恢复汇总            | standalone not-attempted 的 API/CLI 回归返回 recovery_required，全树快照不变                                           | implemented |
| CLI 保密及续查信息      | `test_m3c_execution_outcomes_cli.py` 验证闭合元数据、部分成功、journal 损坏/存在性未知及旧版本兼容                     | verified    |
| 独立审查与消融          | 三路 sol medium 计划审查，授权/逻辑/保密终审；四个新增消融探针与两份既有实验通过                                       | verified    |
| 解耦开发与主 agent 整合 | 三个 luna max 包及中断后的 luna max 收尾；主 agent 汇总并删除重复总状态和薄包装                                        | verified    |
| 全套验证                | 243 项 unittest、Ruff、ty、compileall、uv lock、wheel/sdist 构建、diff check 及完整 hooks                              | verified    |

新增 30 项测试，其中参数化故障注入进一步覆盖多个 journal 阶段。终审要求的
plan/resource preflight 直接故障、当前目标已替换时 failure marking 再次失败，
以及 journal 存在性检查抛错均已补齐。存在性无法确认时输出 null，不伪装成
不存在；稳定输出语义只在 [reference](../reference.md) 定义。

完整 hooks 在 `/tmp/luwu-m3-closure-gate-MJnGeL` 的逐文件一致副本中通过，
包括所有新增未跟踪文件；原工作树受保护的技能文件和 Git 索引未修改。
初次构建/脚本依赖解析因沙箱 DNS 失败，获准重试后通过，不将首次失败记为通过。
构建产物位于 `/tmp/luwu-m3-execution-closure-dist/`；隔离 M1 E2E 位于
`/tmp/luwu-m3-closure-e2e-rvznvC`，最终 in_sync、普通文件权限 0644、无临时残留。

M3 在原冻结范围内完成；M4 本轮未启动。未 commit、push 或改变原工作树的
分支策略；运行探针没有修改真实用户配置。
