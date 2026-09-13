# M3 当前工作树复核与补齐计划

日期：2026-09-13。状态：计划经审查与消融后完成开发及验证。

本轮重新核验 [M3 冻结范围](m3.md)，不沿用旧分支的完成结论。
初始工作树干净；190 项 unittest 通过，原 M3 消融实验的 13 个参考场景通过。
这些结果不覆盖新发现的边界反例。当前优先补齐 M3；M4 仍按
[roadmap](../roadmap.md) 保持后续方向，不在 M3 修复中引入 provider。

## 冻结修复计划

1. journal 的写入范围必须包含固定锁文件：在创建任何 journal/lock 前，
   拒绝二者与 manifest、source、target 的相同路径、解析别名及祖孙重叠。
   复用现有路径判定，仅集中锁文件命名，保留已有 CAS 与进程锁。
1. planner 在既有观察结果的私有内存字段中绑定实际参与分类的 baseline 摘要，
   不将摘要写入输出或 journal；持锁后、replace 前后及最终验证时通过一个
   回调复核 baseline 与 manifest。提交前变化报
   `stale_plan`，提交后变化保留 committed/unknown 语义。复用既有 live 检查。
   不新增持久快照、内容日志或通用事务框架。
1. 为确认的反例增加真实临时目录回归和 CLI 元数据断言；复核没有旁路写入、
   配置值泄漏和错误的成功状态。
1. recovery 的已提交/未变化资源，必须同时满足路径条件与 fresh plan 的
   `in_sync` 才能确认；不能同时输出 `confirmed` 和 `drifted`。未知资源只有
   同样满足当前观察时才标记 `matches_postcondition`，不自动升级为已提交。

## 审查、消融与开发顺序

由独立 sol medium agent 分别审查契约逻辑和保密、授权、文件边界、可验证性。
主 agent 汇总可复现反例与建议，再用生产代码探针验证：删除新 guard 是否重新
出现反例；移除通用 registry、事务及存储抽象是否仍可满足验收。
只保留有反例支撑的边界，不用纯模型实验替代集成测试。

计划冻结后由不同 luna max agent 实现互不重叠的包：

| 包                  | 写集                                                                        | 验收                                                   |
| ------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------ |
| A：journal 写入范围 | `plan_record.py`、独立 execution 测试                                       | 冲突在任何持久写入前阻断，正常执行与 CAS 保持有效      |
| B：反向同步输入绑定 | `mutations.py`、`reconcile.py` 的观察字段与字段 planner、独立 mutation 测试 | baseline/manifest 陈旧状态阻断，提交后失败不误报未写入 |
| C：恢复结果一致性   | `reconcile.py` 的 recovery 分类、独立 recovery 测试                         | 同元数据的实际内容漂移不误报 confirmed；恢复保持零写入 |
| 主 agent：集成      | `reconcile.py` 的 journal 路径预检、文档、消融脚本、CLI 集成测试            | 汇总 review、全套回归、hook gate 与完成逐项审计        |

未证实的候选不作为完成缺口，也不据此添加实现。正式冻结决策、消融结果和验证
证据在本文件补记；稳定契约只修改其 owner `docs/reference.md`，当前状态只修改
`docs/status.md`。

## 本轮 review 汇总与消融结果

两个独立 sol medium 审查分别从契约逻辑与保密/授权/文件边界/可验证性出发，
均真实复现两个阻断：固定锁文件提前创建 0 字节 target；baseline 已变化后仍
反向写入并返回 `committed`。初始成熟度为 M3a implemented、M3b/M3c partial。

采纳两组共同建议：锁文件与 journal 使用同一命名来源，冲突报
`record_path_conflict` 并保持所有文件快照。后续对抗审查发现分类前后夹读还可能
遭遇 A→B→A：分类读取 B，却把授权绑定到 A。因此改为由 planner 绑定实际分类
输入的私有摘要，复用 source/live 既有模式，不新增 snapshot 类型。
manifest 已有外层检查，但临时文件写入期间仍
存在可注入的检查间隙，因此复用回调将其与 baseline 一起覆盖提交前后。

在开发前运行 `uv run experiments/m3_followup_ablation.py`：两个最小 guard
均阻断反例；分别删除 guard 后均重现未经当前授权的写入。探针调用真实生产
writer，仅以短小 wrapper 实验新增检查；不使用 registry、事务、持久快照或
snapshot 类。它证明最小方案可行，不替代随后覆盖 writer 内时序的生产回归。
原 `m3_ablation.py` 的 13 个参考场景及各删除反例也重新通过。

后续保密/可验证性审查又复现 recovery 同时报告 `confirmed` 与 `drifted`：原地
改变 target 为同长度内容并恢复 mtime，既有非内容条件无法区分，而 fresh plan
已经发现漂移。新增第三个消融探针证明利用已有 plan 状态即可阻断误报；删除该
条件则重现，仍不需要持久内容摘要或 recovery engine。此项作为独立 C 包开发。

第三项也由另一 sol medium agent 独立复核：综合判定需当前 `in_sync`，路径级
布尔值继续仅表示非内容条件。采纳其证据边界意见：确认不能证明历史字节未变，
具体限制只写入 reference。C 包由完成 A 的 luna max agent 接续，B 与 C 并行。

实现后的调用点检查又确认 `_write_source` 的旧三个 live 参数与条件分支已无
调用者：统一输入回调承担同样检查。因此将回调设为必需参数，删除旧分支，
再运行既有 live 竞态回归验证等价；不为了内部兼容保留无用接口。

## 完成审计

| 要求                 | 当前证据                                                                                                                   | 结论        |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------- | ----------- |
| M3a 原冻结观察契约   | 原有 manifest、ownership、M3 观察测试继续通过                                                                              | implemented |
| M3b 当前授权绑定     | `test_m3b_input_binding.py` 覆盖分类实际输入、ABA、baseline缺失/不安全、manifest变化与提交后结果                           | implemented |
| M3c journal 写入范围 | `test_m3c_record_paths.py` 覆盖声明角色、别名及祖孙重叠，CLI另证实缺失目标未被锁创建                                       | implemented |
| M3c recovery 一致性  | `test_m3c_recovery_coherence.py` 覆盖 committed/unchanged/unknown 的内容漂移、正常收敛和 not-attempted；unknown 不升级确认 | implemented |
| 输出与副作用边界     | `test_m3_followup_cli.py` 的错误码、已提交未知状态、零写入和摘要/值不泄漏断言                                              | verified    |
| 多视角计划与代码审查 | 独立 sol medium 逻辑及价值 review，主 agent 汇总；最终组合 review 无剩余阻断                                               | verified    |
| 消融后解耦开发       | 两个 luna max agent 分别承担 A/C 与 B；主 agent 集成，删除无调用者的三组 live 参数/分支                                    | verified    |
| 完整回归及交付检查   | 213 项 unittest、两份消融实验、Ruff、ty、compileall、uv lock、构建及 hooks                                                 | verified    |

新增 23 项测试。所有运行数据使用临时目录，未写真实用户配置，也未 commit、push
或变更分支策略。构建产物位于 `/tmp/luwu-m3-followup-dist/`。hooks 对新增未跟踪
文件使用显式 `--files`，不把 `-a` 误当作包含新文件。当前实现状态与验证环境说明
由 [status](../status.md) 维护，恢复的证据限制由 [reference](../reference.md) 维护。

最终全仓 gate 在包含当前全部 tracked/untracked 文件的临时副本中运行，避免
文件修正 hooks 打开原工作树受保护的 `.agents/skills`；原工作树和暂存区不受
影响。副本路径为 `/tmp/luwu-m3-gate-8cZp1I`，逐文件内容一致性另行核对。
