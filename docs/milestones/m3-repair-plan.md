# M3 冻结契约补齐计划

状态：complete，基于 `83ca72e` 的当前工作树修复与最终 gate；已吸收四份独立逻辑/价值审查

## 决策门

初始决策不能进入 M4：M3a 的 v3 只读三方观察已实现；M3b 和 M3c 虽然主链路已经接通，但仍有冻结契约内的边界缺口，因此先完成本计划。修复及全部 gate 已通过，M3b/M3c 现为 `implemented/complete`；M4 的 provider、secret、持久化/缓存、能力和平台工作保持 `unstarted`，不在本计划中提前实现。

初始审查证据包括：

- `src/luwu/reverse_sync.py:38-89` 将完整 source 对象重新编码，未选字段的空白、转义、数值词法和布局可能被改写，而预览只列选中字段；
- `src/luwu/mutations.py:435-459`、`src/luwu/baseline.py:226-268` 和 `src/luwu/reconcile.py:2442-2484` 在 `os.replace` 返回后才标记提交，无法覆盖“替换已发生但调用边界抛错”；
- `src/luwu/plan_record.py:444-560` 只封闭对象键，仍接受空资源和任意非空 `condition.type`；
- 当时 243 项 unittest 与三份既有消融实验通过，但没有覆盖上述反例。

最终实现证据包括：

- `reverse_sync.py` 已改为 literal-JSON 顶层成员的局部 span 补丁，预览输出 `replace/add/delete` 和 separator 元数据，不携带值或 diff；
- baseline、M3b source、M3c target 三个 writer 都在 `os.replace` 边界使用 staged no-follow identity，等值独立目标不会进入 `changed_targets`；
- `PlanRecord` 在 create/from_dict/read/inspect 共用非空、连续 ordinal、闭合 condition 值域校验；
- 全量 unittest 274 项通过，四份 M3 消融、Ruff、ty、compileall、lock/build、临时 M1 CLI 和隔离完整 prek gate 均已通过。

## 冻结的补齐要求

### A：literal-JSON reverse-sync 的实际写入范围

`reverse-sync` 只能改变明确选中的顶层字段值，以及为选中字段增删所必需的局部分隔符。未选中的 `source`、`live`、`merge`、`ignore` 字段和未声明成员的 key 顺序、转义、数字词法、空白和末尾换行必须逐字节保留。严格 JSON、动态 Jinja、嵌套路径和 alias mapping 仍然拒绝；无法安全定位/补丁化的输入必须在写入前返回 `reverse_sync_unsupported`，且预览不得虚报较小的 blast radius。

实现只需要一个 reverse-sync 内部的顶层成员 span 扫描和局部补丁，不引入通用 formatter、通用 JSON 编辑器、span dataclass、schema registry 或第二套语义比较器。现有 `parse_public_object` 仍负责严格值校验；补丁器负责按解码后的唯一 JSON key 绑定成员，保留 source 字节并替换选中的 value span。选中 key 已存在时记录 `replace`；仅 live 有该 key 时记录 `add`；仅 source 有该 key 且 live 删除时记录 `delete`。首项、中间项、末项、唯一项和空对象的分隔符调整必须是封闭的 preview 元数据 `changes`，只包含 key、操作类型和是否调整 separator，不包含值、原文或 diff；扫描结果与严格解析的唯一 key 集合不一致（包括重复解码 key）即在写入前拒绝。

### B：三个 writer 的 replace 不确定性

baseline、M3b source 和 M3c target writer 必须对 `os.replace` 抛错后的状态重新观察 held parent 下的目标。三类 writer 共享同一逻辑矩阵，但不新增跨 writer 的事务/结果类：内部结果只有 `not_replaced`、`replaced`、`indeterminate` 三态。

- writer 在 replace 前记录临时项的 no-follow `(st_dev, st_ino)` 及 link/file 类型。抛错后只有目标 no-follow identity 等于该 staged identity 且临时项已消失，才可认定 `replaced`；这表示目标已继承 staged entry，但目录持久化/后续校验仍是 unknown。仅目标内容、link target 或语义值等于新值，即使临时项消失，也不能证明 Luwu 的替换发生，必须是 `indeterminate`，防止把外部等值写入加入 `changed_targets`。
- 目标仍持有旧 identity 且临时项仍持有原 staged identity，可认定 `not_replaced`；旧值/新值相同、临时项消失但 identity 不匹配、父目录不可观察、两边均不可确认或清理/同步二次失败，均为 `indeterminate`，不得猜测提交事实。
- `not_replaced` 对 mutation 使用 `committed=false`、`outcome=not_committed`；`replaced` 在 replace 已返回时使用 `committed=true`，后续 durability/校验失败使用 `committed_state_unknown`；`indeterminate` 使用 `committed=false`、状态不确定的专用错误码/结果，不得加入 `changed_targets`。
- M3c 的 `indeterminate` 必须传播为当前资源 `unknown`、总体 `recovery_required`、后续资源 `not-attempted`；只有明确 `not_replaced` 且此前没有已知提交时才是 `execution_failed`。`changed_targets` 只包含 identity 已确认替换或 replace 已成功返回的目标。
- 若替换已消耗临时项，清理逻辑不得把 `FileNotFoundError` 二次改写成 cleanup-only 结果；`unlink`、unlock、close 等二次故障不得把已确认 `replaced` 或 `indeterminate` 降级为 `not_replaced`。

M3c 继续 `on_failure = stop`、`rollback = never`；未知资源之后的资源必须保持 `not-attempted`。不新增事务引擎、自动重放、rollback 或不合作 writer 的强一致性承诺。

### C：PlanRecord closed value domain

在 `PlanRecord.create/from_dict/read` 和 `inspect_execution_record` 共用的校验路径中：

- resources 非空，ordinal 必须为 `0..N-1`，`next_ordinal == N`；
- `condition.type` 仅允许当前执行器实际使用的 `missing`、`unsafe`、`regular`、`symlink`、`other`；
- 所有数值严格拒绝 bool；`mode` 限于 `0..0o777`，`size/file_id >= 0`，`mtime_ns` 允许有符号整数；`missing`/`unsafe` 的条件值保持全零，regular/symlink 的 `mtime_ns/file_id == 0` 继续表示现有的通配 postcondition；
- 不改变现有 record schema version、状态转移、metadata-only 和 no-follow 持久化边界。

三态与公共执行边界固定如下，避免 B/C 各自解释：

| writer 事实                              | `ApplyError`/`MutationError`                                                        | `changed_targets` | 资源/计划状态                                                                                                                             |
| ---------------------------------------- | ----------------------------------------------------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `not_replaced`                           | `committed=false`，mutation 为 `not_committed`；target 为普通写失败                 | 不追加            | journal 当前资源为 `unknown`（schema 没有 `failed`）；同次执行的 error context 为 `failed`；无前序提交时计划 `unknown`/`execution_failed` |
| `replaced`，但 durability/后置校验不确定 | `committed=true`，状态不确定                                                        | 追加已确认目标    | 当前资源 `unknown`，计划 `recovery_required`                                                                                              |
| `indeterminate`                          | `committed=false`，target 使用 `recovery_required`；mutation 使用状态不确定 outcome | 不追加            | 当前资源 `unknown`，计划 `recovery_required`                                                                                              |
| 后续资源                                 | 不调用 writer                                                                       | 不变              | `not-attempted`                                                                                                                           |

执行错误的 `committed` 表示“是否存在已知替换”，不表示总体是否需要恢复；`recovery_required` 由资源状态和 journal 事实共同决定。C 必须只在 `reconcile.py` 内接通该传播，不临时改动 `errors.py` 或 `cli.py` 的公共 schema；若实现证明必须扩展公共字段，先在切片结果中报告理由并由主 agent 单独整合。

## 解耦开发切片

各 agent 只修改自己列出的生产文件和新测试文件；不得编辑其他切片的文件、提交或推送。

| 切片     | 写入范围                                                                              | 验收重点                                                                                                                                                                       |
| -------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A        | `src/luwu/reverse_sync.py`、`tests/test_m3b_selective_patch.py`                       | 选中 value 改变/新增/删除；未选和 undeclared 字节逐字节保留；semantic escaped key/嵌套值/首中末分隔符；非严格/动态输入零写入；preview `changes` 范围准确且不泄露值             |
| B        | `src/luwu/baseline.py`、`src/luwu/mutations.py`、`tests/test_m3b_replace_boundary.py` | baseline 与 source 的真实 replace-then-raise、staged identity 三态、旧值相同/等值外部写入/不可观察/cleanup 二次故障，已有 stale/parent 回归不退化                              |
| C        | `src/luwu/reconcile.py`、`tests/test_m3c_replace_boundary.py`                         | target 的真实 replace-then-raise、三态传播、`ApplyError.execution`、JSON/human CLI、journal resource state、recover、changed target 与后续 not-attempted 一致；不改公共 schema |
| D        | `src/luwu/plan_record.py`、`tests/test_m3c_record_schema_closure.py`                  | condition 枚举/值域、非空/连续 ordinal、直接库构造到 read/inspect 的一致拒绝                                                                                                   |
| 主 agent | `experiments/m3_final_closure_ablation.py`、owning docs、集成修正                     | 先做消融，再审查/合并各切片，维护 `docs/status.md`、`docs/reference.md` 与 closure 记录；D 通过 schema 后再让 C 接入最终状态矩阵                                               |

## 验证与消融顺序

1. 计划审查后运行快速消融：只保留三个 guard-on/off probe——A 的字节 span 保留、B/C 的 staged identity 重新观察、D 的闭和值域校验；每个删除都必须重现一个当前反例。脚本只使用临时目录和 monkeypatch，不读取或写入用户配置；完整的 baseline/source/target 故障矩阵留在生产回归测试。
1. 各切片返回后主 agent 检查 diff、结构和切片边界，处理必要的最小交界整合；不接受只增加包装类、通用 registry、第二份状态或与三个反例无关的抽象。
1. 专门故障注入必须使用真实 writer 链路，让 `os.replace` 完成替换后再抛错，并覆盖 baseline/source/target 的 replace 前失败、replace 后抛错、等值独立目标、staged identity、postcondition/fsync/cleanup 不确定。target 至少三资源：N 已知提交、N+1 `indeterminate`、后续 `not-attempted`；同时断言磁盘事实、公共异常、JSON/human CLI、持久 journal、`record-inspect` 和只读 `recover`。
1. 保留由 `83ca72e` 生成的 v5 journal fixture，回归 `from_dict`、`read`、`inspect_execution_record`、`recover`，并显式回归 v1-v4 行为；direct-library `PlanRecord` 构造、`update_path_condition` 和 CLI 读路径必须共用同一 schema 校验。
1. 运行 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v`，三份既有 M3 消融和新的最终消融；再运行 Ruff check/format、ty、compileall、`uv lock --check`、wheel/sdist build、`git diff --check`、隔离完整 hooks 和临时目录 M1 CLI E2E。最终 closure 记录每项 gate 的 HEAD、命令、退出码、测试数、隔离副本一致性和产物路径，并标为 `passed/failed/blocked/unrun`；历史 status 中的通过记录不复用。
1. 只有所有新反例有回归且完整 gate 实际通过后，才把 M3b/M3c 和总 M3 状态恢复为 `implemented/complete`。完成声明必须同时写明：`--yes` 针对的是重新计算后的当前 plan，不是此前 preview 的 exact reviewed-plan consent。

## 不属于本计划

provider/rbw、secret-aware 输入、缓存或 secret baseline、网络/子进程能力、平台扩展、自动 rollback/replay、历史内容证明、用户实际审阅 plan token，以及对忽略 advisory lock 的外部 writer 提供强一致性。这些保留给 M4 或产品后续明确契约。M3 完成不宣称 exact reviewed-plan consent，也不宣称能证明 replace 抛错前后的历史调用事实；只能按上述 identity/journal 边界诚实报告当前状态。

## 审查结论与裁剪

四份独立审查均要求先改计划再开发，确认 A 的字节保留、B/C 的 replace 后观察和 D 的 closed value domain 均不可消融。已采纳的增补是 staged identity、防等值外部写入误判、undeclared 字节矩阵、preview 编辑类型、semantic key 绑定、真实 CLI/journal/recover 验证、旧 v5 fixture 兼容证据和 gate 证据账本。已裁剪的冗余是统一 replace transaction/result class、通用 JSON formatter/editor/AST、span dataclass、schema registry、第二 ledger、自动 recovery engine 和跨 writer 状态抽象。

## 最终 closure 证据

验证基线为工作树父提交 `83ca72e`；直接查询 `origin/master` 返回同一提交，沙盒内 `git fetch origin` 因共享 bare repo 的 `FETCH_HEAD` 写权限被阻塞。验证没有依赖历史 status 的通过声明。

- 全量：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v`，274 tests，exit 0；
- 消融：`m3_ablation.py`、`m3_followup_ablation.py`、`m3_execution_closure_ablation.py`、`m3_final_closure_ablation.py` 均 exit 0；其中缺失依赖的 `uv run` 在授权外部网络重试后通过；
- 静态/构建：Ruff check、Ruff format check、`ty check src tests`、compileall、`uv lock --check`、wheel/sdist build、`git diff --check` 均 exit 0；构建产物位于 `/tmp/luwu-m3-dist.fHihxw/`；
- CLI：临时 M1 fixture 的 `plan -> apply --yes -> inspect` 通过，目标为 regular `0644`、内容正确且无 symlink/temporary entry；
- hook：`/tmp/luwu-m3-gate-submit.7jHFAv` 的完整固定 hook 集合 exit 0，精确复制的 tracked/unignored files 逐文件 byte check exit 0。

因此本计划关闭，M3b/M3c 和总 M3 状态为 `implemented/complete`；M4 仍保持 `unstarted`。
