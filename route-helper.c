/*
 * route-helper.c
 * 通过 PF_ROUTE socket 批量添加/删除 IPv4 路由
 *
 * 用法:
 *   echo "1.2.3.0/24" | sudo route-helper add 172.20.10.1
 *   echo "1.2.3.0/24" | sudo route-helper del
 *
 * stdin: 每行一个 CIDR（由调用方预先过滤注释和空行）
 * stdout: JSON 统计，添加操作还会返回成功写入的路由列表
 * exit: 0=全部成功, 1=部分失败, 2=致命错误
 */

#include <sys/socket.h>
#include <sys/types.h>
#include <net/route.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>

#define BUF_SIZE 512

_Static_assert(sizeof(struct rt_msghdr) + 3 * sizeof(struct sockaddr_in) <= BUF_SIZE,
               "BUF_SIZE too small for route message");

/* ── CIDR 解析 ──────────────────────────────────────────────── */

static int
parse_cidr(const char *cidr, struct in_addr *addr, struct in_addr *mask)
{
    char buf[64];
    strncpy(buf, cidr, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';

    char *slash = strchr(buf, '/');
    if (!slash)
        return -1;
    *slash = '\0';

    char *endptr;
    long prefix_l = strtol(slash + 1, &endptr, 10);
    if (endptr == slash + 1 || *endptr != '\0' || prefix_l < 0 || prefix_l > 32)
        return -1;
    int prefix = (int)prefix_l;

    if (inet_pton(AF_INET, buf, addr) != 1)
        return -1;

    mask->s_addr = (prefix == 0) ? 0 : htonl(~((1u << (32 - prefix)) - 1));

    /* 清除主机位，确保网络地址规范 */
    addr->s_addr &= mask->s_addr;

    return 0;
}

/* ── sockaddr_in 填充 ───────────────────────────────────────── */

static void
fill_sockaddr_in(struct sockaddr_in *sa, struct in_addr addr)
{
    memset(sa, 0, sizeof(*sa));
    sa->sin_len    = sizeof(*sa);
    sa->sin_family = AF_INET;
    sa->sin_addr   = addr;
}

/* ── 构造路由消息 ───────────────────────────────────────────── */
/*
 * 路由消息布局：rt_msghdr + sockaddr_in(dst) [+ sockaddr_in(gw)] + sockaddr_in(mask)
 * sockaddr 的排列顺序由 RTA_ 位掩码决定：DST(0x1) < GATEWAY(0x2) < NETMASK(0x4)
 * del 操作不需要 gateway，所以 mask 紧跟 dst
 */

static int
build_msg(char *buf, int type, int seq,
          struct in_addr dst, struct in_addr *gw, struct in_addr mask)
{
    memset(buf, 0, BUF_SIZE);

    struct rt_msghdr *hdr = (struct rt_msghdr *)buf;
    hdr->rtm_version = RTM_VERSION;
    hdr->rtm_type    = type;
    hdr->rtm_flags   = RTF_UP | RTF_GATEWAY | RTF_STATIC;
    hdr->rtm_seq     = seq;
    hdr->rtm_pid     = getpid();

    char *cp = buf + sizeof(struct rt_msghdr);

    /* RTA_DST */
    fill_sockaddr_in((struct sockaddr_in *)cp, dst);
    cp += sizeof(struct sockaddr_in);

    /* RTA_GATEWAY（仅 add/change） */
    if (gw) {
        fill_sockaddr_in((struct sockaddr_in *)cp, *gw);
        cp += sizeof(struct sockaddr_in);
        hdr->rtm_addrs = RTA_DST | RTA_GATEWAY | RTA_NETMASK;
    } else {
        hdr->rtm_addrs = RTA_DST | RTA_NETMASK;
    }

    /* RTA_NETMASK */
    fill_sockaddr_in((struct sockaddr_in *)cp, mask);
    cp += sizeof(struct sockaddr_in);

    hdr->rtm_msglen = (int)(cp - buf);
    return hdr->rtm_msglen;
}

/* 仅管理本工具拥有的 supplemental resolver，避免改写用户已有的 DNS 设置。 */
static int
update_resolvers(int add)
{
    static const char contents[] =
        "# tailscale-routes\nnameserver 127.0.0.1\nport 15353\n";
    if (add && mkdir("/etc/resolver", 0755) < 0 && errno != EEXIST)
        return 2;

    int dir = open("/etc/resolver", O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
    if (dir < 0)
        return (!add && errno == ENOENT) ? 0 : 2;

    struct stat st;
    if (fstat(dir, &st) < 0 || st.st_uid != 0 || (st.st_mode & 0022)) {
        close(dir);
        return 2;
    }

    char name[256];
    int failures = 0;
    while (fgets(name, sizeof(name), stdin)) {
        /* 域名直接用作文件名。拒绝路径、隐藏文件和未经验证的字节。 */
        size_t n = strcspn(name, "\n");
        if (name[n] != '\n' || n == 0 || n > 253 || name[0] == '.' ||
            name[n - 1] == '.') {
            failures++;
            continue;
        }
        name[n] = '\0';
        if (strspn(name, "abcdefghijklmnopqrstuvwxyz0123456789.-") != n) {
            failures++;
            continue;
        }

        int fd = openat(dir, name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW);
        if (fd >= 0) {
            char current[sizeof(contents)];
            int owned = fstat(fd, &st) == 0 && S_ISREG(st.st_mode) &&
                        st.st_uid == 0 && !(st.st_mode & 0022) &&
                        read(fd, current, sizeof(current)) == sizeof(contents) - 1 &&
                        memcmp(current, contents, sizeof(contents) - 1) == 0;
            close(fd);
            if (add && !owned)
                failures++;
            else if (!add && owned && unlinkat(dir, name, 0) < 0 && errno != ENOENT)
                failures++;
            continue;
        }
        if (errno != ENOENT) {
            failures++;
            continue;
        }

        if (add) {
            /* 先写完整内容再发布，并保留在此期间出现的同名配置。 */
            char temporary[] = "/etc/resolver/.tailscale-routes.XXXXXX";
            fd = mkstemp(temporary);
            if (fd < 0) {
                failures++;
                continue;
            }
            const char *basename = strrchr(temporary, '/') + 1;
            if (write(fd, contents, sizeof(contents) - 1) != sizeof(contents) - 1 ||
                fchmod(fd, 0644) < 0 || linkat(dir, basename, dir, name, 0) < 0)
                failures++;
            close(fd);
            unlinkat(dir, basename, 0);
        }
    }
    close(dir);
    printf("{\"failed\":%d}\n", failures);
    return failures ? 1 : 0;
}

/* ── main ───────────────────────────────────────────────────── */

int
main(int argc, char *argv[])
{
    /* 解析参数 */
    if (argc < 2) {
        fprintf(stderr,
            "Usage: route-helper add <gateway>\n"
            "       route-helper del\n"
            "stdin: one CIDR per line\n");
        return 2;
    }

    if (argc == 2 && strcmp(argv[1], "resolver-add") == 0)
        return update_resolvers(1);
    if (argc == 2 && strcmp(argv[1], "resolver-del") == 0)
        return update_resolvers(0);

    int new_only = (strcmp(argv[1], "add-new") == 0);
    int is_add = new_only || (strcmp(argv[1], "add") == 0);
    int is_del = (strcmp(argv[1], "del") == 0);

    if (!is_add && !is_del) {
        fprintf(stderr, "Unknown action: %s (use 'add' or 'del')\n", argv[1]);
        return 2;
    }

    struct in_addr gw_addr;
    if (is_add) {
        if (argc < 3) {
            fprintf(stderr, "add requires a gateway argument\n");
            return 2;
        }
        if (inet_pton(AF_INET, argv[2], &gw_addr) != 1) {
            fprintf(stderr, "Invalid gateway: %s\n", argv[2]);
            return 2;
        }
    }

    /* 打开路由套接字 */
    int s = socket(PF_ROUTE, SOCK_RAW, AF_INET);
    if (s < 0) {
        perror("socket(PF_ROUTE)");
        return 2;
    }

    /* 禁止回环——不读响应，避免接收缓冲区溢出 */
    int off = 0;
    if (setsockopt(s, SOL_SOCKET, SO_USELOOPBACK, &off, sizeof(off)) < 0)
        perror("warning: setsockopt(SO_USELOOPBACK)");

    char buf[BUF_SIZE];
    char line[256];
    int total = 0, added = 0, changed = 0, deleted = 0, failed = 0;
    int seq = 0;

    if (is_add)
        printf("{\"applied\":[");

    /* 逐行处理 stdin */
    while (fgets(line, sizeof(line), stdin)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0')
            continue;

        struct in_addr net_addr, net_mask;
        if (parse_cidr(line, &net_addr, &net_mask) != 0) {
            fprintf(stderr, "Invalid CIDR: %s\n", line);
            failed++;
            total++;
            continue;
        }

        total++;
        if (new_only && net_mask.s_addr != htonl(UINT32_MAX)) {
            failed++;
            continue;
        }

        /* PF_ROUTE write() 是原子操作，不会出现 partial write */
        if (is_add) {
            int len = build_msg(buf, RTM_ADD, ++seq,
                                net_addr, &gw_addr, net_mask);
            if (write(s, buf, len) < 0) {
                if (errno == EEXIST) {
                    /* DNS 发现的地址可能已有系统路由，保留原路由的控制权。 */
                    if (new_only)
                        continue;
                    /* 路由已存在，降级为 change */
                    len = build_msg(buf, RTM_CHANGE, ++seq,
                                    net_addr, &gw_addr, net_mask);
                    if (write(s, buf, len) < 0) {
                        failed++;
                    } else {
                        printf("%s\"%s/%d\"", (added + changed) ? "," : "",
                               inet_ntoa(net_addr), __builtin_popcount(net_mask.s_addr));
                        changed++;
                    }
                } else {
                    failed++;
                }
            } else {
                printf("%s\"%s/%d\"", (added + changed) ? "," : "",
                       inet_ntoa(net_addr), __builtin_popcount(net_mask.s_addr));
                added++;
            }
        } else {
            /* del */
            int len = build_msg(buf, RTM_DELETE, ++seq,
                                net_addr, NULL, net_mask);
            if (write(s, buf, len) < 0) {
                if (errno != ESRCH) {
                    /* ESRCH = 路由不存在，静默跳过 */
                    failed++;
                }
            } else {
                deleted++;
            }
        }
    }

    close(s);

    /* 输出 JSON 统计 */
    if (is_add)
        printf("],\"total\":%d,\"added\":%d,\"changed\":%d,\"failed\":%d}\n",
               total, added, changed, failed);
    else
        printf("{\"total\":%d,\"deleted\":%d,\"failed\":%d}\n",
               total, deleted, failed);

    return (failed > 0) ? 1 : 0;
}
