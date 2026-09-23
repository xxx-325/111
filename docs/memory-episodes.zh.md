# 连续 commit 记忆 episode

这一可选流程保留连续 first-parent commit 链中的每个原始任务，每个 commit 后可加入
多个相关需求。候选代码、User 和 Code Conversation 持续累积；扩展任务没有可注入的
参考补丁。继续复用现有状态机、发送审核、Judge、沙箱和最终静默接受。

## 准备和运行

以下命令在本仓库根目录、激活其 Python 环境后运行，配置文件内的路径按该工作目录解析。
离线验证运行 `python -m pytest tests/ -q`；调用模型还需要配置好的 Docker 镜像与凭证。

沿用已有渐进配置，设置 `continuous_commits: true`、`progressive_issues: true`，
`tasks` 依次填写每个连续 commit 的完整 SHA，保留 repository、base、模型、镜像和预算。

```bash
python -m simulator.openhands.prepare_scenario \
  --config config.json --env-file provider.env --output runs/scenario-preparation
```

准备器复用 decomposer 模型和 relay，对每个真实 diff、基线的改动文件及前面的设定，
生成一次草稿并只读审核一次。输入、草稿、审核均私有保存；拒绝即停止，不重新抽样。
全部通过后才生成 `frozen/scenario.json`。这一步调用模型，但不启动 Code Agent；
产物只是候选实验设计，不能证明记忆有效。

配置中加入 `scenario_file: "runs/scenario-preparation/frozen/scenario.json"`。
也可以人工编写同一结构，参见[结构示例](../examples/continuous-commit-scenario.json)。
随后冻结展开后的整个需求队列：

```bash
python -m simulator.openhands.prepare_progressive \
  --config config.json --env-file provider.env --output runs/requirements
```

报告通过后设置 `prepared_issues: "runs/requirements/report.json"`，在新目录运行：

```bash
python -m simulator --config config.json --env-file provider.env --output runs/episode
```

这个 scenario CLI 流程成功完成后，自动导出到 `runs/episode-package` 并整理原运行。
原目录保留指向交接包的清理凭据，不再可续跑。暂停、失败或在途的运行保留工作状态；
已有非 scenario 运行不自动获得清理行为。

运行配置固定 scenario 原始字节的哈希；修改设定或运行策略后不能静默恢复旧运行。
不能跳 commit 或注入参考补丁。累计候选可能与预先设计的文件前置条件不同；此时暂停
检查，不覆盖 Agent 已做的修改。

## 信息和释放

事实只保留身份、M1–M6 类型、正文、适用范围、触发条件以及可选的旧事实引用。
M1 外部约定、M2 外部事实、M6 讨论决策留在控制侧/User；只有 M3 仓库误导、
M4 高成本试错、M5 运行时差异可附仓库编辑。编辑保留相对路径、完整前后文本、类型
和理由。写入前核对所有前置条件，写到一半中断不自动重播；应用期间冻结 Code 沙箱。
首次 User 回合、接受后同回合直接发送新任务，都保证在 Code 工作前完成注入。

Judge 获得当前范围内的设定、触发条件和本轮公开提问、工具调用与发给模型的工具结果，
现有只读审核核对触发依据。不能仅因又失败一轮或时间过去就释放受控事实。没有触发的
事实可以一直不出现，不能为暴露它制造错误或延长对话。这是模型语义审核，不保证每次
分类都正确；样本入选前仍须检查真实对话。

User 已知事实跨任务保留；后来的纠正只覆盖其适用范围。User 知道不代表 Code 已听过，
下游 QA 必须在真实公开消息/工具中找到依据。本版不新增环境 Agent；虚构的外部条件
是提前冻结的设定，运行结果仍须来自真实工具，不能编造。M4/M5 如果需要新的依赖或
服务，仍需另行准备执行镜像，本模块不会自动创建这些环境。

## 交给记忆和 QA 的材料

运行停止且工具调用完整配对后导出：

```bash
python -m simulator.openhands.memory_episode \
  --source runs/episode --output runs/episode-package
```

独立导出默认不修改源运行；只有完成的运行可以加 `--compact-source` 执行同样的
核验后清理。自动导出的包已经存在时，不要再次导出到同一目录。

产物集中在新目录：

- `dialogue.jsonl`、`dialogue.json`、`dialogue.html`：公开 User/Code 消息及工具调用、
  工具结果，保留原事件身份和顺序。
- `snapshot/`：累计候选快照，移除 Git、环境目录和生成缓存。
- `manifest.json`：对话字节哈希、截止事件、代码快照指纹。
- `control-config.json`：筛选后的模型/镜像配置，不含凭证。
- 有 scenario 时的 `private/scenario-index.json`：commit、任务、改动路径及公开
  事件导航，不含隐藏事实正文和补丁，也不能作为记忆输入。

整理后，`private/review.json` 保留需求、扰动设定、范围更新、释放历史、状态选择和
Judge 审核/拒绝原因；`private/trace.jsonl.gz` 保存私有 User/Judge 事件、模型响应、
去重后的指令和工具定义、原始终端证据以及 Judge 检查源码。Code 公开工具已在对话里，
不再额外保留重复 SDK 文件或每次请求附带的整段历史。保留请求哈希和用量，但整理后
不能完整还原每次模型上下文或恢复 SDK 会话。

最终代码只保留 `snapshot/`，临时候选/参考副本、缓存、mailbox、密钥及已核验属于
本运行的 Docker 资源会释放。先核对导出对话、代码和压缩证据；工作器仍活跃或校验
失败时不删除源文件。准备阶段保留少量逐 commit 输入/草稿、冻结设定和审核报告，
供调整设计，其 Provider 日志压缩保存一次。运行整理不递归清理外部源码仓库、准备
目录或历史实验。准备命令在保存输入和审核后，只删除自己下载的临时源码镜像。
对话 HTML 与 JSON 是有意保留的查看和程序读取接口。

对话结构为 `model-visible-dialogue-v1`，包含 `id`、`kind`、从 1 开始的 `sequence`、
`timestamp`，以及消息 `text` 或工具 `call_id`/`tool_name` 和 `action`/`text`。
顺序以 sequence 为准，timestamp 是公开记录收集时间。快照指纹使用与 QA 一致的
`relative-path-executable-content-v1`。

工具结果取第一次包含该结果、发向模型的请求，保留压缩前实际发送的文本。
编辑器私有记录的 `old_content`/`new_content` 不会冒充模型收到的短回复；工具错误和
截断标记按原文保留。请求记录证明提交的上下文，不证明 Provider 成功处理了它。
证据缺失、调用未配对、非文本工具内容、运行仍在途时拒绝导出。旧运行目录内仅双方
消息的 `dialogue.json` 不变，新流程使用导出包。

记忆系统只接收公开 dialogue。QA 可从公开事件或相关代码位置出发，但答案必须有截止
时间前的公开依据；后续解题需求位于截止时间后，使用相关历史而不重复构造期任务。
snapshot 和控制配置分别交给任务评测。目前下游对比为无记忆与注入 QA 答案的 oracle
上下文，不是已经测得的真实检索系统效果。

## 已验证的范围

离线测试覆盖连续来源、多扩展、触发释放、范围更新、精确编辑、恢复边界与导出保真；
合成包联调验证 QA 结构和工具可见性。这些不证明真实 Agent 会被误导、发生高成本
试错、仓库独立解题困难或历史遵循提升。仓库编辑和准备审核可以清除直接线索，不能
仅因删除 Git 就宣称所有线索已清除。正式入选需检查真实公开证据和对比实验；
无记忆也能完成不必直接淘汰，少反复澄清、少走失败路线本身也可能有价值。
