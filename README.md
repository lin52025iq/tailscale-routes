# tailscale-routes

[English](README_EN.md) | 中文

macOS 下 Tailscale 使用 exit node 模式时的分流路由管理工具。

## 解决什么问题

Tailscale 开启 exit node 后，会通过两条 `/1` 路由（`0.0.0.0/1` 和 `128.0.0.0/1`）将所有流量导入 VPN 隧道。macOS 不支持 split tunnel（[tailscale/tailscale#13677](https://github.com/tailscale/tailscale/issues/13677)），也没有 Linux 上 `ip rule` + routing table 的对等方案。

本工具利用路由表的**最长前缀匹配**原则，为指定 IP 段添加比 `/1` 更精确的路由（如 `/8`、`/16`、`/24`），使这些网段绕过 VPN，继续走本地物理网关直连。支持一次性管理**数千至上万条路由**，全程自动化。

## 特性

- **高性能批量路由操作**：使用 C 语言编写的路由助手程序，通过 macOS PF_ROUTE socket 直接操作内核路由表，7500 条路由的添加/删除在 150ms 内完成
- **全自动状态管理**：守护进程每 5 秒检测 exit node 状态，连接时自动添加路由，断开时自动清理，切换 WiFi 时自动重建
- **路由文件热更新**：修改 `bypass-routes.txt` 后无需重启守护进程，5 秒内自动检测变更并增量更新（只添加新增的、只删除移除的）
- **崩溃自恢复**：launchd 守护进程配置了 `KeepAlive`，进程异常退出后自动重启，重启时通过 JSON 状态文件精确恢复
- **零外部依赖**：Python 部分只使用标准库（兼容 macOS 自带的 Python 3.9+），C 部分只使用系统头文件，无需安装任何第三方包

## 架构

```
tailscale-routes.py (Python 主控程序)
  ├─ 检测 exit node 状态
  ├─ 获取物理网关 (过滤 Tailscale utun 接口)
  ├─ 监控 bypass-routes.txt 变化 (mtime + set 差集)
  └─ 调用 route-helper (通过 stdin/stdout/exit code 通信)

route-helper (C 语言路由助手)
  ├─ 从 stdin 读取 CIDR 列表
  ├─ 打开 PF_ROUTE socket
  └─ 批量构造 rt_msghdr 写入内核路由表
```

## 前置要求

- macOS（已在 Sequoia 15.x 上测试）
- Tailscale（App Store 或 standalone 版均可）
- Xcode Command Line Tools（提供 C 编译器和 Python 3）
  ```bash
  xcode-select --install
  ```

## 安装

```bash
cd tailscale-routes
./install.sh
```

安装脚本会自动完成：编译 C 路由助手、安装 Python 主程序、配置 sudoers 免密（仅限 route-helper）、注册 launchd 用户级守护进程。安装后即刻生效，无需重启。

<details>
<summary>install.sh 会创建/修改的文件</summary>

| 路径 | 操作 | 说明 |
|------|------|------|
| `/usr/local/bin/route-helper` | 创建 | 编译后的 C 路由助手二进制 |
| `/usr/local/bin/tailscale-routes` | 创建 | Python 主程序 |
| `/usr/local/etc/tailscale-routes.conf` | 创建 | 共享路径配置 |
| `/usr/local/etc/bypass-routes.txt` | 同步 | 以仓库里的 `bypass-routes.local.txt` 为准 |
| `/etc/sudoers.d/tailscale-routes` | 创建 (需 sudo) | 当前用户对 route-helper 的免密执行规则 |
| `~/Library/LaunchAgents/com.local.tailscale-routes.plist` | 创建 | 从模板生成的 launchd 用户级守护进程 |

</details>

## 配置路由

首次运行 `./install.sh` 时，若本地配置缺失，脚本会从 `bypass-routes.example.txt` 创建 `bypass-routes.local.txt`。之后只需编辑 `bypass-routes.local.txt`，再运行 `./install.sh`，脚本会将其同步到 `/usr/local/etc/bypass-routes.txt`。Git 会忽略本地文件。每行可写一个 IPv4 地址、CIDR、精确域名或通配域名，`#` 开始注释。

```
# 私有网段（局域网直连）
10.0.0.0/8
192.168.0.0/16

# 国内公共 DNS
114.114.114.0/24
223.5.5.0/24
119.29.29.0/24
203.0.113.7

# 精确域名和通配域名
api.example.com
*.cdn.example.com
```

`*.cdn.example.com` 匹配任意层级的子域名。若也要覆盖 `cdn.example.com`，把它另写一行。守护进程支持运行配置热更新。所有规则只在 Tailscale 正在连接且选用了 Exit Node 时启用，断开后会清理相应路由和域名解析配置。

域名规则在 macOS 系统 DNS 收到查询时创建 IPv4 路由，并按 DNS TTL 回收。已有缓存、应用自带的加密 DNS 和 IPv6 连接可能不经过这套规则。共用同一 IPv4 地址的域名会走同一条路由。启用域名规则时，程序使用本机 `127.0.0.1:15353`。已有的同名 `/etc/resolver` 项优先保留。

> **注意**：bypass-routes.txt 中的网段不能覆盖探测 IP（默认 `8.8.8.8`，可在 `tailscale-routes.conf` 的 `PROBE_IP` 中修改）。该 IP 用于检测 exit node 是否激活，如果被旁路路由覆盖，检测会失效。程序会自动排除冲突网段并在日志中报错，但建议提前避免。

## 常用命令

| 命令 | 说明 |
|------|------|
| `tailscale-routes status` | 查看守护进程、exit node 状态、网关、活跃路由数、最近日志 |
| `tailscale-routes start` | 启动守护进程 |
| `tailscale-routes stop` | 停止守护进程并清理路由 |
| `tailscale-routes add` | 仅当 Exit Node 激活时启动守护进程 |
| `tailscale-routes remove` | 同 `stop`，停止监控并清理规则 |
| `tail -f /tmp/tailscale-routes.log` | 实时查看守护进程日志 |

## 卸载

```bash
./uninstall.sh
```

卸载脚本会停止守护进程、清理路由和域名解析配置、删除安装文件。`bypass-routes.txt` 配置文件会保留，需要手动删除。

## 文件说明

| 文件 | 说明 |
|------|------|
| `tailscale-routes.py` | Python 主程序：状态机、网关检测、热更新、日志 |
| `route-helper.c` | C 语言路由助手：PF_ROUTE socket 批量路由操作 |
| `bypass-routes.example.txt` | IPv4 和域名分流规则的可提交示例 |
| `bypass-routes.local.txt` | 实际安装的本地规则，Git 忽略 |
| `tailscale-routes.conf` | 所有脚本共享的路径配置 |
| `com.local.tailscale-routes.plist.template` | launchd 守护进程模板 |
| `install.sh` | 一键安装（编译 + 部署 + 注册服务） |
| `uninstall.sh` | 一键卸载 |
| `tests/` | C 集成测试 + Python 单元测试 |

## 安全设计

- sudoers 只授权**当前安装用户**对 `route-helper` 一个二进制的免密执行权限，不使用 setuid
- C 路由助手对 CIDR 输入做严格校验（`strtol` + `inet_pton`），防止恶意输入操作默认路由
- 自动检测并排除覆盖探测 IP 的路由，防止 exit node 检测失效导致路由循环添加/删除
- 以用户级 LaunchAgent 运行（非系统 Daemon），权限范围最小化

## 注意事项

- 日志自动轮转，最多保留 2000 行
- 状态文件 `/tmp/tailscale-routes.state` 为 JSON 格式，记录当前活跃的网关和路由列表，守护进程重启后据此做精确差异比对
- Tailscale 官方 macOS split tunnel 支持仍在开发中（[tailscale/tailscale#13677](https://github.com/tailscale/tailscale/issues/13677)），本工具是等待期间的替代方案
