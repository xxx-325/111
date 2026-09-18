# Agent Session Simulator

从 issue、commit、PR 或 SWE-Chain-Evo 任务生成可审计的多轮软件开发对话。持续的 User Agent 与持续的 Code Agent 交互，私有 Judge 检查当前候选实现并决定是否继续修改。

[English](README.md)

## 功能

- User、Code、Judge 分别使用独立且持续的 OpenHands Conversation。
- 逐步披露需求，不在第一轮直接暴露完整答案。
- 由宿主状态机选择当前合法的用户意图。
- Code 与 Judge 的工具分别运行在 SSH 连接的 Docker 沙箱中。
- 参考修复、未来任务、Judge 证据和模型密钥不进入 Code 工作区。
- 未解决时继续交互，直到接受、确实受阻或达到资源上限。
- 导出只包含 User 与 Code 公开消息的 `dialogue.json` 和 `dialogue.html`。

这是实验性数据生成工具。流程成功并不等于生成对话已经覆盖真实用户的总体分布。

## 目录

```text
simulator/   调度、Agent、状态机、Judge 和导出器
tests/       单元测试与契约测试
docker/      控制环境和执行环境镜像
containers/  OpenHands 控制镜像
examples/    issue 与配置示例
docs/        架构和隔离说明
flow.html    可离线打开的流程概览
```

运行结果、模型日志、参考材料、本地数据集、虚拟环境和 `.env` 都不会提交到 Git。

## 环境要求

- Python 3.12
- Docker
- Git
- 各角色可访问的 OpenAI-compatible 模型接口
- 当前 SSH 沙箱实现支持 macOS 或 Linux 宿主

OpenHands 依赖锁定在 `openhands-requirements.txt` 和 `openhands-linux.lock`。

需要控制镜像时，从仓库根目录构建：

```bash
docker build -f containers/openhands/Dockerfile -t local/session-openhands:1.47.0 .
docker build -f docker/control/Dockerfile -t local/agent-session-control:1.47.0 .
```

## 安装

```bash
python3.12 -m venv .venv-openhands
source .venv-openhands/bin/activate
python -m pip install -r openhands-requirements.txt
export PYTHONPATH=.
```

创建仅保存在本地的 `.env`：

```dotenv
DEEPSEEK_API_KEY=
```

## 准备目标仓库

示例配置使用本地 checkout。以 boltons 为例：

```bash
mkdir -p runs
git clone https://github.com/mahmoud/boltons.git runs/source-boltons
```

配置中的基线和任务引用必须能从该 checkout 解析。参考 commit 只对 Judge 可见，不会应用到 Code 的候选实现。

## 运行

```bash
OPENHANDS_SUPPRESS_BANNER=1 python -m simulator \
  --config examples/openhands-progressive-single.json \
  --env-file .env \
  --output runs/example
```

只在确认运行安全停止且策略兼容时恢复：

```bash
OPENHANDS_SUPPRESS_BANNER=1 python -m simulator \
  --config examples/openhands-progressive-single.json \
  --env-file .env \
  --output runs/example \
  --resume
```

导出纯公开对话：

```bash
python -m simulator.openhands.dialogue_export runs/example
```

导出器生成 `dialogue.json` 和 `dialogue.html`。checkpoint、工具事件、Judge 证据和模型请求仍保存在被 Git 忽略的运行目录中。

## 渐进流程

1. 准备当前任务，将原始信息拆成较小的现象／可能原因片段。
2. 每轮最多新增一个片段。
3. User 从合法状态中选择动作并生成自然请求。
4. Code 只在候选沙箱中检查和修改代码。
5. Judge 在独立副本中检查候选与当前参考。
6. 未解决时，只把用户可观察的失败投影给 User，随后继续。
7. 已解决时，User 可以接受，也可以先自然追问实现细节。
8. 只有当前任务接受后才释放下一任务。

隔离边界见 [docs/execution-isolation.zh.md](docs/execution-isolation.zh.md)。

## 配置要点

- `runtime: "openhands"`：启用持续 OpenHands Conversation。
- `progressive_issues: true`：启用片段释放和 Judge 推进。
- `execution_backend: "ssh_sandbox"`：当前隔离运行必须使用。
- `dialogue_language`：控制公开语言；代码和原始报错保持原样。
- `max_seconds`：限制总运行时间；触限只保存现场，不会伪造完成。
- User、Code、Judge、decomposer 可以分别配置模型和密钥环境变量。
- `code_prompt_mode: "sdk_default"`：Code 使用 SDK 默认角色提示词。

示例只包含公开任务元数据。请按本机环境替换 checkout 路径、镜像 digest 和模型设置。

## 测试

```bash
OPENHANDS_SUPPRESS_BANNER=1 python -m unittest discover -s tests
```

Docker 集成测试为显式启用项，需要兼容的本地镜像：

```bash
SIMULATOR_DOCKER_TEST_IMAGE=your-image \
  OPENHANDS_SUPPRESS_BANNER=1 \
  python -m unittest discover -s tests
```

## 安全与数据边界

- 不要提交 API key、私钥、provider 原始日志或真实用户 transcript。
- 隔离模式下 User 没有仓库工具。
- Code 只能看到可写候选和公开 User 消息。
- Judge 看到只读候选／参考副本，并拥有独立的可写检查目录。
- 沙箱限制 Agent 工具面，但不能完全防御恶意候选代码。
- 即使参考 patch 被隔离，模型也可能从预训练中识别公开任务。

## License

MIT，见 [LICENSE](LICENSE)。
