# 执行隔离

OpenHands 控制运行时与项目工具运行时属于不同的信任边界。候选
`PYTHONPATH` 只选择导入路径；它不会隐藏其它已安装的源文件。较早的共享容器实验不能证明源代码隔离。

## 角色

| 角色 | 模型上下文 | 可执行工作区 |
| --- | --- | --- |
| User | 已发布需求、公开对话、经过审核的观察结果 | 无；只有主机对话控制 |
| Code | 公开 User 消息及其自身工作 | Candidate，可读写 |
| Judge | 当前完整需求及既有检查 | 独立的 candidate 与当前 reference，只读；私有检查可读写 |

只有主机拥有后续任务、provider 凭据和权威任务进度。接受任务需要当前 Judge verdict 与 User 决策。Reference 代码绝不会应用到 Code 的 candidate。

## 工具边界

`ssh_sandbox` 后端将 SDK 对话、持久化和模型访问保留在控制容器中。Terminal 与文件操作通过 SSH/SFTP 发送到独立的项目容器。项目镜像必须包含所需的语言/测试依赖，不能包含 SDK 或另一个已安装版本的目标项目。运行时配置固定镜像 digest。

每个角色都有独立的 SSH 身份和内部网络。模型进程没有宿主 Docker socket、provider key、SDK 日志、其它角色的 reference 材料或外部网络访问。Code 与 Judge 以非 root 身份执行。浏览器及其它未适配工具必须 fail closed；适配器失败绝不能在本地执行该动作。

任务跟踪是角色本地的结构化状态，不是任意文件访问。可读的长输出文件只属于该角色的工具输出目录。原始 SDK 日志始终留在控制侧。

## 连续性与失败处理

OpenHands 继续拥有 agent loop 和上下文压缩。持久远程 shell 保留 cwd 与环境；文件编辑历史支持 undo。这些功能需要真实适配器测试，不能只匹配方法名。

冻结 Code 的 snapshot 时，也要冻结其执行容器：只停止控制进程并不能阻止后台 candidate 变更。Judge 的 candidate 与 reference 挂载保持只读。

恢复必须核对记录的镜像、挂载、角色、执行身份和交付位置。不确定的命令不得再次提交。较早的共享容器 checkpoint 保留，不静默升级。

## 模型运行前所需证据

- 实际 terminal 与文件工具都无法读取控制侧和跨角色 sentinel，包括通过 symlink 和 `/proc` 的访问。
- 目标项目从 candidate 源码导入；干净环境没有已安装的替代目标版本。
- 外部、跨角色、模型 relay 和 Docker socket 访问均失败。
- 编辑、测试、输入、超时、继续、后台、undo 和安全重启均可工作。
- 暂停的 Code 后台进程不能改变 Judge 检查的版本。
- 适配器传输失败会被保留，且不会在本地执行或重播。

将命令和结果与公开对话分开保存。测试找不到一个已知路径，不能证明不存在所有 sandbox escape。容器隔离也不能阻止预训练模型识别公开任务。

实际实现和 probe 结果记录在每次运行的私有 artifacts 中；本架构说明本身不证明某次运行已经通过认证。

运行 `python -m simulator.openhands.sandbox_probe --output runs/isolation-probe`
为准备使用的镜像和宿主生成隔离证据。没有等价独立 executor 的 browser 及其它
工具仍不在该边界内。
