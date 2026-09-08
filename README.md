# M7 双账号管理器

Windows 本地桌面原型，通过两个独立的 Linux Docker 容器运行 March7thAssistant 的云游戏模式。默认串行，支持双账号并行、每日定时、二维码展示、停止与超时控制。

当前版本为 **0.1 开发预览**。自动化逻辑在上游镜像内，本程序管理容器与账号数据；正常退出显示“完成情况未确认”，不把容器退出当成游戏任务全部成功。

如果 Docker Desktop 在 `initializing Inference manager` 阶段报错，可先退出 Docker Desktop，再在设置文件 `%APPDATA%\Docker\settings-store.json` 中将 `EnableDockerAI` 设为 `false` 后重启。修改前请备份该文件；不要使用“恢复出厂设置”，以免清除已有环境。

## 安装与运行

需要 Python 3.12 或以上，以及已启动 Linux 容器引擎的 Docker Desktop。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m m7manager
```

也可双击 `start.cmd`（首次启动会在项目目录创建虚拟环境、安装依赖）。

开发时可使用独立数据目录：

```powershell
.\.venv\Scripts\python.exe -m m7manager --data-dir .dev\accounts
.\.venv\Scripts\python.exe -m m7manager --data-dir .dev\doctor --doctor
```

默认数据保存在 `%LOCALAPPDATA%\M7AccountManager`。同一目录只允许运行一个管理器。仅连接本地 Docker，不使用远程 `DOCKER_HOST` 或 CLI context。

## 首次使用

1. 启动 Docker Desktop，确认处于 Linux 容器模式。
2. 添加两个不同名称的账号槽位。
3. 点击“准备官方镜像”。首次拉取镜像可能较大；程序记录官方 RepoDigest，之后按该 digest 运行。
4. 对第一个账号点击“初始化并运行”，使用目标账号的米游社扫码。扫码后会继续所选任务，并非仅登录。
5. 第一个账号结束后，初始化另一个账号，核对实际游戏账号正确。
6. 在“设置”中启用每日定时。默认时间为北京时间 04:15 / 04:20，默认并发 1。

镜像拉取成功不等于已完成上游兼容性验证，首次真实任务需要核对日志和实际游戏结果。程序不自动充值；每个账号独立使用自己的云游戏权益。锄大地和旧版模拟宇宙不在本版本范围内。

关闭窗口隐藏到托盘；通过托盘“停止任务并退出”结束。退出会等待受管任务停止；Docker 连接中断时需要恢复 Docker 才能确认退出。电脑休眠或关机时不保证执行。

## 已实现

- 独立账号配置、浏览器目录、日志，以及每账号一个活动任务的 SQLite 约束。
- 固定镜像、单次运行、容器标签校验、只管理本安装创建的容器。
- 持久化手动队列与每日调度、两小时错过补跑窗口、并发限制。
- 停止、总超时、OOM 与非零退出识别，以及创建/启动阶段崩溃后的协调恢复。
- 运行中设置延迟应用，容器移除后合并最新配置，保留上游任务进度。
- 桌面账号卡片、实时日志尾部、二维码、运行历史、托盘提示。
- 诊断摘要导出：仅元数据，不包括日志、二维码、账号名称或浏览器凭据。

## 当前边界

- 需要完成真实 Docker + 双账号扫码验证后才能认定部署可用。
- 业务结果保守显示“未确认”；日志目前仅用于阶段及部分异常提示，不验证所有任务已完成。
- 为防止未知错误反复消耗权益，暂不自动重试；用户可以手动重试。
- 每次实时采集保留 stdout/stderr 各 2,000 行尾部，结束时保留各 10,000 行，单流最多 2MB；不承诺完整日志。
- 管理器运行记录、备份和历史文件目前不自动清理；长期使用需手动管理磁盘。上游日志保留天数仍有效。
- 配置模板固定于上游提交 `5e70b026…`；拉取的新镜像与模板需实际验证。暂不提供一键回滚或自动配置迁移。
- 升级/回滚、按任务识别业务成功、长期压力测试、安装包发布属于下一阶段。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
```

核心测试使用受控 Docker 替身，覆盖异常生命周期和调度。真实 Docker 集成测试需显式执行：

```powershell
$env:M7_TEST_DOCKER = "1"
.\.venv\Scripts\python.exe -m pytest -m docker
```

这不会登录游戏，测试使用独立标签和临时目录。测试环境镜像需预先按集成测试说明准备。

完整设计见 `docs/双账号Docker管理器开发方案.md`，阶段记录见 `docs/开发进度.md`。

## 来源与许可证

本项目使用 GPL-3.0-only。`src/m7manager/resources/config.example.yaml` 来自 [March7thAssistant](https://github.com/moesnow/March7thAssistant/blob/5e70b0261a99f6666e99a64a0ffae7488db0fa2b/assets/config/config.example.yaml)，保留上游配置注释；完整许可证见 `LICENSE`。
