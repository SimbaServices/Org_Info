#!/usr/bin/env python3
"""SSH to inventory hosts and write inventory/live/<id>.json snapshots."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("PyYAML is required: pip install -r tools/requirements.txt\n")
    raise

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tools" / "host_snapshot.py"
LIVE_DIR = ROOT / "inventory" / "live"


def load_hosts(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("hosts") or [])


def write_known_hosts(hosts: list[dict], dest: Path) -> Path:
    lines = []
    for host in hosts:
        ssh = host.get("ssh") or {}
        key = ssh.get("host_key")
        address = ssh.get("address") or host.get("ipv4")
        port = int(ssh.get("port") or 22)
        if not (key and address):
            continue
        if port == 22:
            lines.append(f"{address} {key}")
        else:
            lines.append(f"[{address}]:{port} {key}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def ssh_snapshot(host: dict, known_hosts: Path, timeout: int) -> dict:
    ssh = host.get("ssh") or {}
    address = ssh.get("address") or host.get("ipv4")
    user = ssh.get("user") or "root"
    port = str(ssh.get("port") or 22)
    script = SNAPSHOT.read_bytes()
    target = f"{user}@{address}"
    base = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        f"UserKnownHostsFile={str(known_hosts.resolve()).replace(chr(92), '/')}",
        "-o",
        "IdentitiesOnly=yes",
        "-p",
        port,
        target,
    ]
    commands = []
    if user != "root":
        commands.append(base + ["sudo", "-n", "python3", "-"])
    commands.append(base + ["python3", "-"])
    last_err = ""
    for cmd in commands:
        proc = subprocess.run(
            cmd,
            input=script,
            capture_output=True,
            timeout=timeout + 20,
            check=False,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        err = (proc.stderr or b"").decode("utf-8", errors="replace")
        if proc.returncode == 0 and out.strip().startswith("{"):
            return json.loads(out)
        last_err = err or out or f"exit {proc.returncode}"
    raise RuntimeError(last_err.strip() or "ssh snapshot failed")


def collect(hosts: list[dict], timeout: int) -> dict[str, dict]:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    known = write_known_hosts(hosts, LIVE_DIR / "known_hosts")
    results: dict[str, dict] = {}
    for host in hosts:
        if host.get("kind") != "ubuntu" or host.get("edge_only"):
            continue
        if not (host.get("ssh") or {}).get("address") and not host.get("ipv4"):
            continue
        hid = host["id"]
        try:
            snap = ssh_snapshot(host, known, timeout)
            snap["host_id"] = hid
            snap["ok"] = True
            results[hid] = snap
            print(f"live {hid}: ok hostname={snap.get('hostname')}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — one host must not stop the rest
            results[hid] = {
                "host_id": hid,
                "ok": False,
                "error": str(exc),
            }
            print(f"live {hid}: {exc}", file=sys.stderr)
        (LIVE_DIR / f"{hid}.json").write_text(
            json.dumps(results[hid], indent=2) + "\n",
            encoding="utf-8",
        )
    (LIVE_DIR / "latest.json").write_text(
        json.dumps(results, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


def load_saved() -> dict[str, dict]:
    latest = LIVE_DIR / "latest.json"
    if latest.is_file():
        data = json.loads(latest.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    out = {}
    for path in LIVE_DIR.glob("*.json"):
        if path.name in {"latest.json"}:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("host_id"):
            out[data["host_id"]] = data
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", type=Path, default=ROOT / "inventory" / "hosts.yaml")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()
    collect(load_hosts(args.hosts), args.timeout)
    return 0


if __name__ == "__main__":
    # Windows OpenSSH wants a path known_hosts; IdentitiesOnly still uses default keys.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
