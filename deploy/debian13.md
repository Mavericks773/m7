# Debian 13.2 服务器部署准备

本目录提供服务器预览版的 systemd 单元和部署流程。管理器直接运行在 Debian 宿主机的 Python 虚拟环境中，游戏任务运行在同一台机器的本机 Docker Engine 容器中；不开放 Web 端口，也不使用远程 Docker。

## 1. 主机准备

以下命令以 root 或具备 sudo 权限的管理员执行。请先确认云主机具备足够的磁盘、内存和稳定访问官方镜像与云游戏服务的网络；双账号真实运行仍需单独试跑验证。

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates
```

安装 Docker Engine 时请遵循 Docker 官方 Debian 安装文档。安装完成后确认：

```bash
sudo systemctl enable --now docker
sudo docker info --format '{{.OSType}}'
```

## 2. 服务用户与代码

Docker socket 等价于较高的本机控制权限，仅将服务用户加入 `docker` 组，不要把 socket 暴露到公网：

```bash
sudo useradd --system --create-home --home-dir /var/lib/m7manager --shell /usr/sbin/nologin m7manager
sudo usermod -aG docker m7manager
sudo install -d -o m7manager -g m7manager -m 0700 /var/lib/m7manager
sudo install -d -o root -g root -m 0755 /opt/m7manager
sudo git clone <项目地址> /opt/m7manager
sudo python3 -m venv /opt/m7manager/.venv
sudo /opt/m7manager/.venv/bin/python -m pip install --upgrade pip
sudo /opt/m7manager/.venv/bin/pip install -e /opt/m7manager
sudo chown -R root:root /opt/m7manager
```

如果代码目录由服务用户维护，可将最后一行改为 `chown -R m7manager:m7manager /opt/m7manager`；升级前应停止服务并检查代码版本。

## 3. 安装并启动服务

```bash
sudo install -o root -g root -m 0644 deploy/systemd/m7-manager.service \
  /etc/systemd/system/m7-manager.service
sudo systemctl daemon-reload
sudo systemctl enable --now m7-manager
sudo systemctl status m7-manager
sudo journalctl -u m7-manager -f
```

服务启动失败时先看 `journalctl`，再执行环境诊断。`Wants=docker.service` 只表达启动顺序和弱依赖，Docker 暂时不可用时 daemon 仍应能启动并报告降级状态。

## 4. 首次初始化

CLI 必须使用与 systemd 相同的数据目录和运行时目录；SSH 断开不会结束 daemon 或已经持久化的容器任务：

```bash
M7='/opt/m7manager/.venv/bin/m7-manager'
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager --json doctor
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager status
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager image prepare
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager job status <JOB_ID>
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager account add '账号 A'
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager account add '账号 B'
```

首次业务验收顺序：分别手动运行两个账号并导出二维码，用 SFTP 下载二维码扫码；核对容器、账号目录和日志隔离后，再设置每日计划：

```bash
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager login qr <ACCOUNT_ID> --output /tmp/m7-login.png
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager run <ACCOUNT_ID> --task main
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager schedule set <ACCOUNT_ID> --time 04:15 --enabled true
```

`run` 返回的是已入队的 trigger ID，不代表任务已开始或业务已完成。使用 `status`、`logs` 和 `history` 核对真实状态；正常容器退出仍显示业务结果未确认。

## 5. 维护、备份与升级

正常 `systemctl restart` 会保存排队项和活动任务状态，不会替用户执行 `stop --all`。需要停掉游戏时先明确停止并确认，再停服务：

```bash
sudo -u m7manager $M7 --data-dir /var/lib/m7manager --runtime-dir /run/m7manager stop --all
sudo systemctl stop m7-manager
```

完整备份前停止任务和服务，并同时保存 SQLite、账号配置、浏览器目录和运行产物。数据库 schema 迁移会在数据目录 `backups/` 中使用 SQLite 在线备份；不要只复制 WAL 主文件。

```bash
sudo tar --xattrs --acls -czf /root/m7manager-$(date +%F).tar.gz \
  /var/lib/m7manager
```

升级时先停服务、备份、替换代码并重新执行 editable 安装，再启动并检查日志。不要让旧版本直接打开不兼容的新 schema；回滚时使用与旧程序配套的完整备份。

## 6. 安全边界

- Unix socket 默认位于 `/run/m7manager/control.sock`，目录 `0700`、socket `0600`，不监听公网。
- 不设置 `DOCKER_HOST`，管理器只连接 `/var/run/docker.sock`。
- 不把二维码、浏览器资料或账号配置写入日志；导出的二维码文件默认 `0600` 且不覆盖已有文件。
- `m7manager` 加入 `docker` 组后具有较高权限，请限制 SSH 用户、sudo 权限和云安全组入站规则。
