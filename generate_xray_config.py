#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 VLESS 链接生成 Xray 客户端配置（输出到 stdout）。
用法: VLESS_NODE="vless://..." python3 generate_xray_config.py > config.json
"""

import json
import os
import sys
import urllib.parse


def parse_vless(vless_url):
    """解析 VLESS 分享链接为节点字典"""
    if not vless_url or not vless_url.startswith("vless://"):
        print("❌ 无效的 VLESS 链接", file=sys.stderr)
        sys.exit(1)

    url = vless_url[len("vless://"):]

    if "#" in url:
        url, name = url.split("#", 1)
        name = urllib.parse.unquote(name)
    else:
        name = "VLESS-Node"

    if "@" not in url:
        print("❌ 缺少 @ 分隔符", file=sys.stderr)
        sys.exit(1)
    uuid, rest = url.split("@", 1)

    if "?" in rest:
        address_port, query = rest.split("?", 1)
        params = dict(urllib.parse.parse_qsl(query))
    else:
        address_port, query = rest, ""
        params = {}

    if ":" in address_port:
        address, port = address_port.rsplit(":", 1)
        port = int(port)
    else:
        address, port = address_port, 443

    return {
        "name": name,
        "uuid": uuid,
        "address": address,
        "port": port,
        "encryption": params.get("encryption", "none"),
        "security": params.get("security", ""),
        "network": params.get("type", "tcp"),
        "ws_path": params.get("path", "/"),
        "ws_host": params.get("host", address),
        "sni": params.get("sni", address),
        "fingerprint": params.get("fp", "chrome"),
        "flow": params.get("flow", ""),
        "pbk": params.get("pbk", ""),
        "sid": params.get("sid", ""),
    }


def generate_xray_config(vless_url):
    node = parse_vless(vless_url)

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "port": 10808,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "streamSettings": {"network": "tcp"},
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": node["address"],
                            "port": node["port"],
                            "users": [
                                {
                                    "id": node["uuid"],
                                    "encryption": node["encryption"],
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": node["network"],
                    "security": node["security"],
                },
            }
        ],
    }

    if node["flow"]:
        config["outbounds"][0]["settings"]["vnext"][0]["users"][0]["flow"] = node["flow"]

    ss = config["outbounds"][0]["streamSettings"]

    if node["network"] == "ws":
        ss["wsSettings"] = {
            "path": node["ws_path"],
            "headers": {"Host": node["ws_host"]},
        }
    elif node["network"] == "grpc":
        ss["grpcSettings"] = {
            "serviceName": node["ws_path"].lstrip("/") if node["ws_path"] else ""
        }

    if node["security"] == "tls":
        ss["tlsSettings"] = {
            "serverName": node["sni"],
            "fingerprint": node["fingerprint"],
            "allowInsecure": False,
        }
    elif node["security"] == "reality":
        ss["realitySettings"] = {
            "serverName": node["sni"],
            "fingerprint": node["fingerprint"],
            "publicKey": node["pbk"],
            "shortId": node["sid"],
            "allowInsecure": False,
        }

    return config


def main():
    vless_url = os.environ.get("VLESS_NODE", "")
    if not vless_url:
        print("❌ 错误: 未设置 VLESS_NODE 环境变量", file=sys.stderr)
        sys.exit(1)
    try:
        print(json.dumps(generate_xray_config(vless_url), indent=2))
    except Exception as e:
        print(f"❌ 生成配置失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
