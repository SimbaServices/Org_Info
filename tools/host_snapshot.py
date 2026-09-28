#!/usr/bin/env python3
"""Collect nginx, ufw, and listener facts on the machine that runs this script.

Prints one JSON object to stdout. Safe to run as root via SSH.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LISTEN_RE = re.compile(
    r"LISTEN\s+\S+\s+\S+\s+(\S+):(\d+)\s",
)
UFW_ALLOW_RE = re.compile(r"^(\S+)\s+ALLOW")
PROXY_RE = re.compile(r"proxy_pass\s+(\S+);")
LISTEN_DIR_RE = re.compile(r"listen\s+([^;]+);")
SERVER_NAME_RE = re.compile(r"server_name\s+([^;]+);")
LOCATION_RE = re.compile(r"location(?:\s+\S+)?\s+(\S+)\s*\{")
INCLUDE_RE = re.compile(r"include\s+([^;]+);")
KEEP_UNITS = (
    "nginx",
    "docker",
    "containerd",
    "postgresql",
    "postgres",
    "govdeals",
    "ledger",
    "maintenance",
    "wellnav",
    "propeval",
    "energy",
    "caddy",
    "traefik",
    "uvicorn",
    "gunicorn",
)


def run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return ""
    return (proc.stdout or "") + (proc.stderr if proc.returncode and not proc.stdout else "")


def parse_ss(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        match = LISTEN_RE.search(line)
        if not match:
            continue
        addr, port_s = match.group(1), match.group(2)
        port = int(port_s)
        clean = addr.replace("%lo", "")
        public = clean not in {
            "127.0.0.1",
            "::1",
            "[::1]",
            "127.0.0.53",
            "127.0.0.54",
        } and not clean.startswith("127.0.0.53")
        if clean.endswith("%lo"):
            public = False
        rows.append({"addr": clean, "port": port, "public": public})
    return rows


def parse_ufw(text: str) -> dict:
    allows = []
    active = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Status:"):
            active = "active" in line.lower()
        match = UFW_ALLOW_RE.match(line)
        if match:
            allows.append(match.group(1))
    return {"active": active, "allows": allows, "raw_ok": bool(text.strip())}


def _tokens(value: str) -> list[str]:
    return [part for part in re.split(r"\s+", value.strip()) if part]


def _read_includes(pattern: str) -> str:
    import glob

    chunks = []
    for path in sorted(glob.glob(pattern.strip())):
        try:
            chunks.append(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def parse_nginx(text: str) -> dict:
    servers: list[dict] = []
    orphans: list[dict] = []
    if not text.strip():
        return {"ok": False, "servers": [], "orphan_locations": []}
    depth = 0
    in_server = False
    server: dict | None = None
    loc: dict | None = None
    loc_depth = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            opens = line.count("{")
            closes = line.count("}")
            depth += opens - closes
            continue
        if line.startswith("server") and "{" in line and depth <= 2:
            server = {"listen": [], "server_names": [], "locations": []}
            in_server = True
        if in_server and server is not None:
            listen_m = LISTEN_DIR_RE.search(line)
            if listen_m:
                bits = _tokens(listen_m.group(1))
                spec = bits[0] if bits else ""
                ssl = "ssl" in bits
                default = "default_server" in bits
                if ":" in spec:
                    addr, _, port_s = spec.rpartition(":")
                    addr = addr.strip("[]") or "*"
                    port = int(re.sub(r"[^\d]", "", port_s) or "0")
                else:
                    addr = "*"
                    digits = re.sub(r"[^\d]", "", spec)
                    port = int(digits) if digits else (443 if ssl else 80)
                server["listen"].append(
                    {"addr": addr, "port": port, "ssl": ssl, "default": default}
                )
            name_m = SERVER_NAME_RE.search(line)
            if name_m:
                server["server_names"].extend(
                    n for n in _tokens(name_m.group(1)) if n != ";"
                )
            inc_m = INCLUDE_RE.search(line)
            if inc_m and loc is None:
                extra = parse_nginx(_read_includes(inc_m.group(1)))
                for extra_server in extra.get("servers") or []:
                    server["locations"].extend(extra_server.get("locations") or [])
                # Snippets are often bare location blocks, not a full server.
                if extra.get("orphan_locations"):
                    server["locations"].extend(extra["orphan_locations"])
            loc_m = LOCATION_RE.search(line)
            if loc_m and loc is None:
                loc = {"path": loc_m.group(1), "proxy_pass": None}
                loc_depth = depth + line.count("{") - line.count("}")
            proxy_m = PROXY_RE.search(line)
            if proxy_m and loc is not None:
                loc["proxy_pass"] = proxy_m.group(1).rstrip(";")
        elif not in_server:
            loc_m = LOCATION_RE.search(line)
            if loc_m and loc is None:
                loc = {"path": loc_m.group(1), "proxy_pass": None}
                loc_depth = depth + line.count("{") - line.count("}")
            proxy_m = PROXY_RE.search(line)
            if proxy_m and loc is not None:
                loc["proxy_pass"] = proxy_m.group(1).rstrip(";")
        opens = line.count("{")
        closes = line.count("}")
        depth += opens - closes
        if loc is not None and depth < loc_depth:
            if server is not None:
                server["locations"].append(loc)
            else:
                orphans.append(loc)
            loc = None
        if in_server and depth == 0 and server is not None:
            servers.append(server)
            server = None
            in_server = False
            loc = None
    return {"ok": True, "servers": servers, "orphan_locations": orphans}


def parse_units(text: str) -> list[dict]:
    units = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("UNIT"):
            continue
        name = line.split()[0] if line.split() else ""
        if not name.endswith(".service"):
            continue
        low = name.lower()
        if any(token in low for token in KEEP_UNITS):
            units.append({"name": name, "line": " ".join(line.split()[:4])})
    return units


def main() -> int:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        nginx = run(["sudo", "-n", "nginx", "-T"]) or run(["nginx", "-T"])
    else:
        nginx = run(["nginx", "-T"])
        if not nginx.strip():
            nginx = run(["sudo", "-n", "nginx", "-T"])
    ss_out = run(["ss", "-lnt"])
    if not ss_out.strip():
        ss_out = run(["ss", "-lntn"])
    ufw_out = run(["ufw", "status"])
    if not ufw_out.strip():
        ufw_out = run(["sudo", "-n", "ufw", "status"])
    units_out = run(
        [
            "systemctl",
            "list-units",
            "--type=service",
            "--state=running",
            "--no-pager",
            "--no-legend",
        ]
    )
    payload = {
        "schema": 1,
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "hostname": socket.gethostname(),
        "ss": parse_ss(ss_out),
        "ufw": parse_ufw(ufw_out),
        "nginx": parse_nginx(nginx),
        "units": parse_units(units_out),
    }
    dest = os.environ.get("SIMBA_NETWORK_LIVE")
    text = json.dumps(payload, indent=2) + "\n"
    print(text, end="")
    if dest:
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
