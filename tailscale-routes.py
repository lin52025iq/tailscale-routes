#!/usr/bin/python3
"""
tailscale-routes - macOS Tailscale exit node bypass route manager

在 Tailscale exit node 模式下，让指定 IPv4 和域名绕过 VPN 走本地网关直连。
通过 route-helper C 程序批量操作 PF_ROUTE socket。

用法:
    tailscale-routes watch    - 守护进程模式（默认）
    tailscale-routes start    - 启动守护进程
    tailscale-routes stop     - 停止守护进程并清理路由
    tailscale-routes add      - Exit Node 启用时启动守护进程
    tailscale-routes remove   - 停止守护进程并清理路由
    tailscale-routes status   - 查看当前状态
"""

import argparse
import fcntl
import ipaddress
import json
import logging
import os
import re
import select
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# ── 配置加载 ─────────────────────────────────────────────────

def load_config(conf_path=None):
    """从 tailscale-routes.conf 加载共享路径配置"""
    if conf_path is None:
        candidates = [
            Path("/usr/local/etc/tailscale-routes.conf"),
            Path(__file__).resolve().parent / "tailscale-routes.conf",
        ]
        for p in candidates:
            if p.exists():
                conf_path = p
                break
        else:
            print("❌ 找不到 tailscale-routes.conf", file=sys.stderr)
            sys.exit(1)

    config = {}
    with open(conf_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                val = val.split("#")[0].rstrip()  # 去除行内注释
                config[key.strip()] = val
    return config


# ── 日志 ─────────────────────────────────────────────────────

def setup_logging(log_file, max_lines=2000):
    """配置日志：写文件 + 轮转"""
    logger = logging.getLogger("tailscale-routes")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

    # 简单轮转：超过 max_lines 时截断前半
    def rotate():
        try:
            with open(log_file) as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                with open(log_file, "w") as f:
                    f.writelines(lines[max_lines // 2:])
        except OSError:
            pass

    logger.rotate = rotate
    return logger


# ── 网关检测 ─────────────────────────────────────────────────

def get_gateway():
    """
    获取本地物理网关 IP。
    从 netstat -rnf inet 过滤 utun 接口，取第一条物理 default 路由的网关。
    不用 scutil：exit node 激活后 Global/IPv4 返回的是 VPN 网关。
    """
    try:
        result = subprocess.run(
            ["netstat", "-rnf", "inet"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if (len(parts) >= 4
                    and parts[0] == "default"
                    and not parts[3].startswith("utun")):
                try:
                    ipaddress.ip_address(parts[1])
                    return parts[1]
                except ValueError:
                    continue
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ── Exit node 检测 ───────────────────────────────────────────

def _is_tailscale_running():
    """检查 Tailscale 进程是否在运行（App Store 版或 standalone 版）"""
    try:
        # GUI 可以在网络扩展仍运行时退出。使用完整进程名匹配两种安装方式。
        pattern = (
            r"(^|/)(Tailscale|tailscaled|IPNExtension|"
            r"io\.tailscale\.ipn\.macsys\.network-extension)( |$)"
        )
        result = subprocess.run(
            ["pgrep", "-if", pattern],
            capture_output=True, timeout=3
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def is_exit_node_active(probe_ip="8.8.8.8"):
    """
    判断 Tailscale exit node 是否激活。
    先检查 Tailscale 进程是否在运行，再检查探测 IP 的路由是否走 utun。
    双重检查避免其他 VPN 的 utun 接口造成误判。

    probe_ip 不能被 bypass-routes.txt 覆盖，否则检测会失效。
    """
    if not _is_tailscale_running():
        return False
    try:
        result = subprocess.run(
            ["route", "-n", "get", probe_ip],
            capture_output=True, text=True, timeout=5
        )
        return re.search(r"^\s*interface:\s*utun[0-9]+\s*$", result.stdout, re.MULTILINE) is not None
    except (subprocess.TimeoutExpired, OSError):
        return False


def should_apply_routes(probe_ip):
    """同时核对本机路由和 Tailscale 的配置，其他 VPN 也可能占用 utun。"""
    if not is_exit_node_active(probe_ip):
        return False
    # LaunchAgent 的 PATH 通常不包含 Homebrew 或 Tailscale 的命令目录。
    cli = shutil.which("tailscale") or next(
        (path for path in ("/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                           "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
         if os.access(path, os.X_OK)),
        None,
    )
    if cli is None:
        return False
    try:
        result = subprocess.run(
            [cli, "debug", "prefs"], check=True,
            capture_output=True, text=True, timeout=3,
        )
        prefs = json.loads(result.stdout)
        if not isinstance(prefs, dict) or prefs.get("WantRunning") is not True:
            return False
        if not (prefs.get("ExitNodeID") or prefs.get("ExitNodeIP") or prefs.get("AutoExitNode")):
            return False

        # 使用后端状态确认连接，避免其他 VPN 的 utun 恰好匹配探测地址。
        result = subprocess.run(
            [cli, "status", "--json"], check=True,
            capture_output=True, text=True, timeout=3,
        )
        status = json.loads(result.stdout)
        return isinstance(status, dict) and status.get("BackendState") == "Running"
    except (subprocess.SubprocessError, OSError, ValueError):
        return False


# ── 路由文件加载 ─────────────────────────────────────────────

def load_rules(routes_file, probe_ip=None):
    """读取同一个文件里的 IPv4、精确域名和通配域名。"""
    routes, exact, wildcards = set(), set(), set()
    logger = logging.getLogger("tailscale-routes")
    try:
        probe = ipaddress.IPv4Address(probe_ip) if probe_ip else None
    except ValueError:
        probe = None

    try:
        with open(routes_file, encoding="utf-8-sig") as f:
            for raw in f:
                rule = raw.partition("#")[0].strip()
                if not rule:
                    continue
                try:
                    net = ipaddress.ip_network(rule, strict=False)
                except ValueError:
                    wildcard = rule.startswith("*.")
                    name = rule.removeprefix("*.").removesuffix(".")
                    try:
                        name = name.encode("idna").decode("ascii").lower()
                    except UnicodeError:
                        name = ""
                    # 只接受带后缀的主机名，避免 URL、IP 拼写错误或系统级 DNS 后缀。
                    if (len(name) <= 253 and "." in name
                            and re.fullmatch(r"[a-z0-9.-]+", name)
                            and not name.rsplit(".", 1)[-1].isdigit()
                            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                                                 label) for label in name.split("."))):
                        (wildcards if wildcard else exact).add(name)
                    else:
                        logger.warning(f"⚠️ 无效 CIDR 或域名，跳过: {rule}")
                    continue

                if net.version != 4:
                    logger.warning(f"⚠️ 仅支持 IPv4，跳过: {rule}")
                elif probe and probe in net:
                    logger.error(f"❌ 路由 {net} 覆盖了探测 IP {probe_ip}，已排除")
                else:
                    routes.add(str(net))
    except (OSError, UnicodeError) as e:
        logger.error(f"❌ 路由文件读取失败: {e}")
    return routes, exact, wildcards


def load_routes(routes_file, probe_ip=None):
    """返回规则文件里的静态 IPv4 路由。"""
    return load_rules(routes_file, probe_ip)[0]


def get_file_mtime(path):
    """获取文件 mtime，不存在返回 0"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ── Route Helper 调用 ────────────────────────────────────────

def call_route_helper(helper_path, action, cidrs, gateway=None):
    """
    调用 C route-helper 程序。
    返回 (success: bool, result: dict)
    """
    logger = logging.getLogger("tailscale-routes")
    cmd = ["sudo", helper_path, action]
    if gateway:
        cmd.append(gateway)

    stdin_data = "\n".join(cidrs) + "\n"

    try:
        result = subprocess.run(
            cmd, input=stdin_data,
            capture_output=True, text=True, timeout=30
        )
        if result.stdout.strip():
            stats = json.loads(result.stdout.strip())
        else:
            stats = {}
        return result.returncode <= 1, stats
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        logger.error(f"❌ route-helper 调用失败: {e}")
        return False, {}


# ── 状态管理 ─────────────────────────────────────────────────

@contextmanager
def _state_lock(state_file, timeout=0):
    # 前台 watch 和 LaunchAgent 共用系统路由，状态文件必须只有一个写入进程。
    fd = os.open(state_file + ".lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def load_state(state_file):
    """从状态文件加载 JSON，返回 dict 或 None"""
    try:
        with open(state_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_state(state_file, gateway, routes, mtime):
    """保存状态到 JSON 文件（原子写入：write-then-rename）"""
    logger = logging.getLogger("tailscale-routes")
    state = {
        "gateway": gateway,
        "routes": sorted(routes),
        "mtime": mtime,
    }
    tmp = state_file + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, state_file)
    except OSError as e:
        logger.error(f"❌ 状态文件写入失败: {e}")


def clear_state(state_file):
    """删除状态文件"""
    try:
        os.remove(state_file)
    except OSError:
        pass


# ── 路由操作 ─────────────────────────────────────────────────

def add_routes(config, gateway, cidrs, *, new_only=False):
    """返回成功写入的路由，便于部分失败时仍能准确清理。"""
    if not gateway or not cidrs:
        return set()

    _, stats = call_route_helper(
        config["ROUTE_HELPER"], "add-new" if new_only else "add", sorted(cidrs), gateway
    )
    applied = set(stats.get("applied", [])) & cidrs
    if applied or stats.get("failed") or not stats:
        logging.getLogger("tailscale-routes").info(
            f"➕ 已写入 {len(applied)} 条旁路路由，失败 {stats.get('failed', '未知')} 条"
        )
    return applied


def remove_routes(config, cidrs):
    """删除旁路路由"""
    logger = logging.getLogger("tailscale-routes")
    logger.info("➖ 删除旁路路由")
    success, stats = call_route_helper(
        config["ROUTE_HELPER"], "del", cidrs
    )
    total = stats.get("total", 0)
    failed = stats.get("failed", 0)
    if failed > 0:
        logger.info(f"⚠️  路由删除完成（共 {total} 条，{failed} 条失败）")
    else:
        logger.info(f"✅ 路由删除完成（共 {total} 条）")
    return success and not failed


# ── 按域名解析及动态路由 ──────────────────────────────────────

DNS_PORT = 15353
RESOLVER_CONTENTS = f"# tailscale-routes\nnameserver 127.0.0.1\nport {DNS_PORT}\n"


def _sync_resolvers(config, names):
    """恢复或更新本工具的 macOS 域名解析规则。"""
    directory = Path("/etc/resolver")
    current = set()
    try:
        marker = RESOLVER_CONTENTS.encode()
        for path in directory.iterdir():
            if path.name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as f:
                    if f.read(len(marker) + 1) == marker:
                        current.add(path.name)
            except (FileNotFoundError, PermissionError):
                # 其他服务的配置可能在扫描期间变化，或不对当前用户开放。
                continue
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.getLogger("tailscale-routes").error(f"❌ 读取域名解析配置失败: {e}")
        return False

    ok = True
    for action, changed in (("resolver-del", current - names), ("resolver-add", names - current)):
        if changed:
            success, stats = call_route_helper(config["ROUTE_HELPER"], action, sorted(changed))
            if not success or stats.get("failed") or "failed" not in stats:
                logging.getLogger("tailscale-routes").warning(
                    f"⚠️ 无法更新部分域名解析配置: {action}"
                )
                ok = False
    return ok


def _dns_name(packet, offset):
    labels = []
    end = None
    limit = len(packet)
    size = 0
    while offset < limit:
        length = packet[offset]
        if length == 0:
            return ".".join(labels).lower(), end if end is not None else offset + 1
        if length & 0xc0 == 0xc0 and offset + 1 < limit:
            pointer = ((length & 0x3f) << 8) | packet[offset + 1]
            # 压缩指针只能引用前面的字段，并且指针链也必须严格向前。
            if pointer >= offset:
                raise ValueError("无效 DNS 压缩指针")
            if end is None:
                end = offset + 2
            limit, offset = offset, pointer
            continue
        if length > 63 or offset + length + 1 > limit:
            raise ValueError("无效 DNS 标签")
        label = packet[offset + 1:offset + length + 1].decode("ascii")
        if "." in label:
            raise ValueError("DNS 标签含有点")
        labels.append(label)
        size += length + 1
        if size > 254:
            raise ValueError("DNS 名称过长")
        offset += length + 1
    raise ValueError("DNS 名称不完整")


def _dns_question(packet):
    if struct.unpack_from("!H", packet, 4)[0] != 1:
        raise ValueError("仅支持单问项 DNS 报文")
    name, offset = _dns_name(packet, 12)
    kind, rr_class = struct.unpack_from("!HH", packet, offset)
    return name, kind, rr_class, offset + 4


def _dns_addresses(response, question):
    """仅为答案中属于查询名或其 CNAME 链的 IPv4 地址添加路由。"""
    flags, _, count = struct.unpack_from("!HHH", response, 2)
    name, kind, rr_class, offset = _dns_question(response)
    if (name, kind, rr_class) != question[:3] or not flags & 0x8000:
        raise ValueError("DNS 响应与查询不匹配")
    if flags & 0x020f or kind not in (1, 255) or rr_class != 1:
        return {}

    aliases, addresses = {}, {}
    for _ in range(count):
        owner, offset = _dns_name(response, offset)
        rr_type, rr_class, ttl, size = struct.unpack_from("!HHIH", response, offset)
        start = offset + 10
        offset = start + size
        if offset > len(response):
            raise ValueError("DNS 资源记录不完整")
        if rr_class == 1 and rr_type == 5:
            target, end = _dns_name(response, start)
            if end != offset:
                raise ValueError("无效 CNAME")
            aliases[owner] = (target, ttl)
        elif rr_class == 1 and rr_type == 1 and size == 4:
            addr = socket.inet_ntoa(response[start:offset])
            addresses.setdefault(owner, {})[addr + "/32"] = ttl

    alias_ttl = 0xffffffff
    visited = set()
    while name in aliases:
        if name in visited:
            return {}
        visited.add(name)
        name, ttl = aliases[name]
        alias_ttl = min(alias_ttl, ttl)
    return {route: min(ttl, alias_ttl) for route, ttl in addresses.get(name, {}).items()}


def _dns_upstreams():
    # 读取系统默认上游，直接发包以避免沿 /etc/resolver 递归到自己。
    try:
        with open("/etc/resolv.conf", encoding="ascii") as f:
            return [fields[1] for line in f if len(fields := line.split()) >= 2
                    and fields[0] == "nameserver"]
    except (OSError, UnicodeError):
        return []


def _read_dns_over_tcp(sock, timeout):
    # 以整条消息为单位限制耗时，避免慢连接阻塞 Exit Node 状态轮询。
    deadline = time.monotonic() + timeout
    data = bytearray()
    size = 2
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock.settimeout(remaining)
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
        if len(data) == 2:
            size += struct.unpack("!H", data)[0]
    return bytes(data[2:])


def _relay_dns(query, upstreams, tcp):
    for host in upstreams[:2]:
        try:
            addr = ipaddress.ip_address(host)
            family = socket.AF_INET if addr.version == 4 else socket.AF_INET6
            with socket.socket(family, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                sock.connect((host, 53))
                if tcp:
                    sock.sendall(struct.pack("!H", len(query)) + query)
                    response = _read_dns_over_tcp(sock, 1)
                else:
                    sock.sendall(query)
                    response = sock.recv(65535)
            if len(response) >= 12 and response[:2] == query[:2]:
                return response
        except (EOFError, OSError, ValueError, struct.error):
            continue
    return None


class _DnsUdpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        query, sock = self.request
        response = self.server.answer(query, False)
        if response:
            sock.sendto(response, self.client_address)


class _DnsTcpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            query = _read_dns_over_tcp(self.request, 0.5)
            response = self.server.answer(query, True)
            if response:
                self.request.settimeout(0.5)
                self.request.sendall(struct.pack("!H", len(response)) + response)
        except (EOFError, OSError):
            pass


# ── 守护进程主循环 ───────────────────────────────────────────

POLL_INTERVAL = 5
STABILIZE_WAIT = 2


def watch(config):
    """在同一事件循环串行处理 DNS 回包、路由变更和状态持久化。"""
    logger = logging.getLogger("tailscale-routes")
    logger.info("🚀 守护进程启动")

    routes_file = config["ROUTES_FILE"]
    state_file = config["STATE_FILE"]
    probe_ip = config.get("PROBE_IP", "8.8.8.8")
    state = load_state(state_file) or {}
    gateway = state.get("gateway")
    active_routes = set(state.get("routes", []))
    static, exact, wildcards = set(), set(), set()
    leases = {}
    upstreams = []
    listeners = []
    mtime = None
    active = False
    serving = False
    stopping = False
    tailnet_cidr = ipaddress.IPv4Network("100.64.0.0/10")

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def matches(name):
        return name in exact or any(name.endswith("." + suffix) for suffix in wildcards)

    def sync_routes():
        now = time.monotonic()
        for key, deadline in list(leases.items()):
            if deadline <= now or not matches(key[0]):
                del leases[key]
        dynamic = {route for _, route in leases}
        # 静态规则保留原有行为，DNS 动态规则只创建空闲的 /32。
        applied = add_routes(config, gateway, static - active_routes)
        applied |= add_routes(config, gateway, dynamic - static - active_routes, new_only=True)
        if applied:
            active_routes.update(applied)
            save_state(state_file, gateway, active_routes, mtime)
        obsolete = active_routes - static - dynamic
        if obsolete and remove_routes(config, obsolete):
            active_routes.difference_update(obsolete)
            save_state(state_file, gateway, active_routes, mtime)

    def answer(query, tcp):
        try:
            question = _dns_question(query)
            response = _relay_dns(query, upstreams, tcp)
            if response is not None:
                host = question[0]
                addresses = _dns_addresses(response, question)
                if (addresses and serving and not stopping and matches(host)
                        and should_apply_routes(probe_ip) and get_gateway() == gateway):
                    now = time.monotonic()
                    for route, ttl in addresses.items():
                        ip = ipaddress.IPv4Network(route).network_address
                        # 避免接管 Tailscale 自身的地址、探测地址或本机特殊地址。
                        if (str(ip) not in (probe_ip, gateway) and ip not in tailnet_cidr
                                and not (ip.is_loopback or ip.is_link_local or ip.is_multicast
                                         or ip.is_unspecified or ip.is_reserved)):
                            key = (host, route)
                            leases[key] = max(leases.get(key, 0), now + max(ttl, 1))
                    # 应用收到地址后可能立刻连接，必须先完成路由更新。
                    sync_routes()
                return response
            return query[:2] + b"\x81\x82\x00\x01" + bytes(6) + query[12:question[3]]
        except (IndexError, ValueError, struct.error):
            return None

    next_poll = 0
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stopping:
            if time.monotonic() >= next_poll:
                enabled = should_apply_routes(probe_ip)
                new_gateway = get_gateway() if enabled else None
                if enabled and not active and new_gateway:
                    time.sleep(STABILIZE_WAIT)
                    enabled = should_apply_routes(probe_ip)
                    new_gateway = get_gateway() if enabled else None
                if stopping:
                    break
                if active_routes and (not enabled or (new_gateway and new_gateway != gateway)):
                    if remove_routes(config, active_routes):
                        active_routes.clear()
                        clear_state(state_file)
                if not enabled:
                    leases.clear()

                names = set()
                serving = False
                active = enabled
                if enabled and new_gateway and (not active_routes or new_gateway == gateway):
                    gateway = new_gateway
                    current_mtime = get_file_mtime(routes_file)
                    if current_mtime != mtime:
                        static, exact, wildcards = load_rules(routes_file, probe_ip)
                        mtime = current_mtime
                    upstreams = _dns_upstreams() if exact or wildcards else []
                    sync_routes()
                    if upstreams and not listeners:
                        try:
                            listeners.append(socketserver.UDPServer(("127.0.0.1", DNS_PORT), _DnsUdpHandler))
                            listeners.append(socketserver.TCPServer(("127.0.0.1", DNS_PORT), _DnsTcpHandler))
                            for listener in listeners:
                                listener.answer = answer
                        except OSError as e:
                            for listener in listeners:
                                listener.server_close()
                            listeners.clear()
                            logger.warning(f"⚠️ 本地域名解析无法启动: {e}")
                    if upstreams and listeners:
                        names = exact | wildcards
                        serving = True

                # DNS 配置只在监听器就绪后写入，关闭时先卸载配置再释放端口。
                if _sync_resolvers(config, names) and not names:
                    for listener in listeners:
                        listener.server_close()
                    listeners.clear()
                logger.rotate()
                next_poll = time.monotonic() + POLL_INTERVAL

            wait = max(0, next_poll - time.monotonic())
            if listeners:
                ready, _, _ = select.select(listeners, [], [], wait)
                for listener in ready:
                    listener.handle_request()
            else:
                time.sleep(wait)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        _sync_resolvers(config, set())
        for listener in listeners:
            listener.server_close()
        if active_routes and remove_routes(config, active_routes):
            clear_state(state_file)


# ── 状态查看 ─────────────────────────────────────────────────

def status(config):
    """打印当前状态"""
    probe_ip = config.get("PROBE_IP", "8.8.8.8")
    label = config.get("PLIST_LABEL", "com.local.tailscale-routes")
    active = should_apply_routes(probe_ip)
    gw = get_gateway()

    # 检查守护进程是否在运行
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5
        )
        daemon_running = label in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        daemon_running = False

    print(f"守护进程状态   : {'✅ 运行中' if daemon_running else '⭕ 未运行'}")
    print(f"Exit node 状态 : {'✅ 已激活' if active else '⭕ 未激活'}")
    print(f"当前网关       : {gw or '（获取失败）'}")

    state = load_state(config["STATE_FILE"])
    if state:
        print(f"已记录网关     : {state.get('gateway', '—')}")
        print(f"活跃路由条数   : {len(state.get('routes', []))}")
    else:
        print("已记录网关     : （无，路由未添加）")

    routes, exact, wildcards = load_rules(config["ROUTES_FILE"], probe_ip)
    print(f"配置路由条数   : {len(routes)}")
    print(f"域名规则条数   : {len(exact)} 条精确，{len(wildcards)} 条通配")

    print()
    print("最近 10 条日志：")
    log_file = config["LOG_FILE"]
    try:
        with open(log_file) as f:
            lines = f.readlines()
            for line in lines[-10:]:
                print(line, end="")
    except OSError:
        print("（暂无日志）")


# ── 守护进程启停 ─────────────────────────────────────────────

def _get_plist_path(config):
    """推导 launchd plist 安装路径"""
    label = config.get("PLIST_LABEL", "com.local.tailscale-routes")
    return os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")


def daemon_stop(config):
    """停止守护进程并清理路由"""
    logger = logging.getLogger("tailscale-routes")
    plist = _get_plist_path(config)
    label = config.get("PLIST_LABEL", "com.local.tailscale-routes")

    # 先停守护进程
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5
        )
        if label in result.stdout:
            r = subprocess.run(
                ["launchctl", "unload", plist],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                print("✅ 守护进程已停止")
                logger.info("🔌 守护进程被 stop 命令停止")
            else:
                print(f"⚠️  停止守护进程失败: {r.stderr.strip()}")
                return False
        else:
            print("⭕ 守护进程未在运行")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"⚠️  停止守护进程失败: {e}")
        return False

    try:
        with _state_lock(config["STATE_FILE"], timeout=5):
            success = _sync_resolvers(config, set())
            state = load_state(config["STATE_FILE"])
            if state and state.get("routes"):
                if remove_routes(config, state["routes"]):
                    clear_state(config["STATE_FILE"])
                    print("✅ 路由已清理")
                else:
                    print("⚠️ 路由清理失败，状态文件已保留，可重试 tailscale-routes stop")
                    success = False
            return success
    except OSError as e:
        print(f"⚠️ 无法取得路由状态锁，请确认前台 watch 已退出: {e}")
        return False


def daemon_start(config):
    """启动守护进程"""
    logger = logging.getLogger("tailscale-routes")
    plist = _get_plist_path(config)
    label = config.get("PLIST_LABEL", "com.local.tailscale-routes")

    if not os.path.exists(plist):
        print(f"❌ plist 不存在: {plist}")
        print("   请先执行 ./install.sh 安装")
        return

    # 检查是否已在运行
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5
        )
        if label in result.stdout:
            print("⭕ 守护进程已在运行")
            return
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        r = subprocess.run(
            ["launchctl", "load", plist],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            print("✅ 守护进程已启动")
            logger.info("🚀 守护进程被 start 命令启动")
        else:
            print(f"❌ 启动失败: {r.stderr.strip()}")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"❌ 启动失败: {e}")


# ── 入口 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="macOS Tailscale exit node bypass route manager"
    )
    parser.add_argument(
        "action", nargs="?", default="watch",
        choices=["watch", "start", "stop", "add", "remove", "status"],
        help="操作：watch(默认) | start | stop | add | remove | status"
    )
    args = parser.parse_args()

    config = load_config()
    setup_logging(config["LOG_FILE"])
    probe_ip = config.get("PROBE_IP", "8.8.8.8")

    if args.action == "watch":
        try:
            with _state_lock(config["STATE_FILE"]):
                watch(config)
        except BlockingIOError:
            print("⭕ 已有进程管理旁路路由")
            sys.exit(1)
    elif args.action == "start":
        daemon_start(config)
    elif args.action in ("stop", "remove"):
        if not daemon_stop(config):
            sys.exit(1)
    elif args.action == "add":
        # 由守护进程执行，确保 Exit Node 关闭后也能自动撤销手动启用的规则。
        if not should_apply_routes(probe_ip):
            print("⭕ Tailscale Exit Node 未激活，未添加路由")
            return
        daemon_start(config)
    elif args.action == "status":
        status(config)


if __name__ == "__main__":
    main()
