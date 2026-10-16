"""acp —— ACP Management CLI（15.4.3，按 15.4.2 冻结合同实现）。

单文件、stdlib only、零打包：拷下来即可运行，requirements 零改动。

    python cli/acp_cli.py [GLOBAL] <resource> <action> [ARGS]

资源：users / agents / channels / identities / connections / status
动作：list / get / create（status 无动作；P2 的 send / update / delete 不在本步）

冻结合同（15.4.2）：
- 输入 canonical = flags（嵌套 config 走 --config <json>）；--json 仅控制输出，
  不控制输入；
- 输出默认人类可读；--json 直接透传 API 响应（不加工、不裁剪字段）；
- status 只透传既有事实字段（processing_status / delivery_status /
  failure_stage / failure_code / retryable），绝不合成 overall status；
  recent 区域只用 interactions list 已有字段（13.1：payload 只在详情）；
- exit：0 成功；1 network/transport failure；2 argparse/客户端校验/API 4xx；
  3 API 5xx；4 资源不存在（404）；
- Excluded：init / start / update / delete / wecom webhook / health 命令 /
  SDK / package & entry point / 新 API / 新 schema / config file / 向导。

边界：CLI 是既有 HTTP API 的薄消费者——适配 API，而不是 API 适配 CLI
（15.4.1 裁决）；CLI 标志名 == API JSON 字段名（snake_case 镜像，零翻译）。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8000"
TIMEOUT_S = 5
STATUS_RECENT_LIMIT = 5  # 冻结：status recent 取最近 5 条（<= API 上限 200）

# resource -> (API path, create 字段序, 人类列表显示列)
# create 字段名 == API JSON 字段名；config 表示嵌套 JSON 对象（--config 传入）。
RESOURCES = {
    "users": ("/api/users", ("name",),
              ("id", "name", "status")),
    "agents": ("/api/agents", ("name", "type", "config"),
               ("id", "name", "type", "status")),
    "channels": ("/api/channels", ("name", "type", "config"),
                 ("id", "name", "type", "status")),
    "identities": ("/api/identities", ("user_id", "channel_id", "external_id"),
                   ("id", "user_id", "channel_id", "external_id", "status")),
    "connections": ("/api/connections", ("channel_id", "agent_id", "name"),
                    ("id", "name", "channel_id", "agent_id", "status")),
}


def fail(code: int, message: str) -> None:
    """错误出口：一律 stderr，stdout 只承载正常结果（管道安全）。"""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def request(method: str, url: str, payload=None, not_found: str | None = None):
    """一次 HTTP 调用；非 2xx 按冻结合同映射 exit code（不返回）。

    transport 失败（连接拒绝/超时/DNS）→ 1；404 → 4；4xx → 2；5xx → 3。
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:  # API 错误体是 {"detail": ...}；原样透出，不做二次解释
            detail = json.loads(detail).get("detail", detail)
        except (ValueError, AttributeError):
            pass
        if e.code == 404:
            fail(4, not_found or detail or f"HTTP 404 ({url})")
        if 400 <= e.code < 500:
            fail(2, f"HTTP {e.code}: {detail}")
        fail(3, f"HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        fail(1, f"cannot reach {url} ({e})")


def print_table(rows, fields) -> None:
    """最小对齐表（stdlib 手写，不引依赖；空集打印占位而非静默）。"""
    if not rows:
        print("(empty)")
        return
    widths = {f: max(len(f), *(len(str(r.get(f, ""))) for r in rows))
              for f in fields}
    header = "  ".join(f.ljust(widths[f]) for f in fields)
    print(header)
    print("  ".join("-" * widths[f] for f in fields))
    for r in rows:
        print("  ".join(str(r.get(f, "")).ljust(widths[f]) for f in fields))


def do_status(args) -> None:
    """status = /health + 五实体计数 + 最近 5 条 Interaction（事实透传）。"""
    health = request("GET", args.base.rstrip("/") + "/health")
    counts = {}
    for name, (path, _, _) in RESOURCES.items():
        listing = request("GET", args.base.rstrip("/") + path)
        counts[name] = listing.get("total", len(listing.get("items", [])))
    recent = request(
        "GET", f"{args.base.rstrip('/')}/api/interactions?limit={STATUS_RECENT_LIMIT}")
    items = recent.get("items", [])

    if args.as_json:
        print(json.dumps(
            {"health": health, "counts": counts, "recent": items}))
        return

    print("ACP Status")
    print("----------")
    ok = isinstance(health, dict) and health.get("status") == "ok"
    print(f"{'Health':<13}{'OK' if ok else health}")
    for name in RESOURCES:
        print(f"{name.capitalize():<13}{counts[name]}")
    print()
    print("Recent Interactions")
    print("-------------------")
    if not items:
        print("(none)")
    for it in items:  # 只用 list 既有事实字段；不合成 overall status
        line = (f"#{it.get('id')}  {it.get('processing_status')}"
                f" / {it.get('delivery_status')}")
        if it.get("failure_stage"):
            line += f"  {it['failure_stage']} {it.get('failure_code') or ''}".rstrip()
            if it.get("retryable"):
                line += " (retryable)"
        print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acp", description="ACP Management CLI (15.4.3)")
    parser.add_argument("--base", default=os.environ.get("ACP_BASE_URL", DEFAULT_BASE),
                        help=f"API base URL (default {DEFAULT_BASE}, env ACP_BASE_URL)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="machine-readable output (API response passthrough)")
    sub = parser.add_subparsers(dest="resource", required=True)

    for name, (path, create_fields, _) in RESOURCES.items():
        p = sub.add_parser(name)
        actions = p.add_subparsers(dest="action", required=True)
        actions.add_parser("list")
        get_p = actions.add_parser("get")
        get_p.add_argument("id", type=int)
        create_p = actions.add_parser("create")
        for field in create_fields:
            if field == "config":
                create_p.add_argument(
                    "--config", default=None,
                    help='nested config as JSON object, e.g. \'{"token": "..."}\'')
            else:
                # CLI 标志 == API 字段（snake_case 镜像）：--user-id -> user_id
                create_p.add_argument("--" + field.replace("_", "-"), required=True)

    sub.add_parser("status")
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)

    if args.resource == "status":
        do_status(args)
        return

    path, create_fields, display = RESOURCES[args.resource]
    base = args.base.rstrip("/")

    if args.action == "list":
        data = request("GET", base + path)
        if args.as_json:
            print(json.dumps(data))
        else:
            print_table(data.get("items", []), display)
    elif args.action == "get":
        data = request("GET", f"{base}{path}/{args.id}",
                       not_found=f"{args.resource} {args.id} not found")
        if args.as_json:
            print(json.dumps(data))
        else:
            print_table([data], display)
    else:  # create
        payload = {}
        for field in create_fields:
            value = getattr(args, field)
            if value is None:
                continue
            if field == "config":
                try:
                    value = json.loads(value)
                except ValueError as e:
                    fail(2, f"invalid --config JSON: {e}")
                if not isinstance(value, dict):
                    fail(2, "--config must be a JSON object")
            payload[field] = value
        data = request("POST", base + path, payload)
        if args.as_json:
            print(json.dumps(data))
        else:
            print_table([data], display)


if __name__ == "__main__":
    main()
