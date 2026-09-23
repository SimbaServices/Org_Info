"""Reconcile declared network.yaml inventory with live host snapshots."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False


def _hostname(route: dict) -> str:
    url = (route.get("url") or "").strip()
    name = (route.get("name") or "").strip()
    if url.startswith("http"):
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = (parsed.path or "").rstrip("/")
        return f"{host}{path}" if path and path != "/" else host
    if "/" in name and not name.startswith("/"):
        return name.rstrip("/")
    return name.split(":")[0]


def _bind_port(bind: str | None) -> int | None:
    if not bind:
        return None
    tail = str(bind).rsplit(":", 1)[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    return int(digits) if digits else None


def _proxy_port(proxy: str | None) -> int | None:
    if not proxy:
        return None
    cleaned = proxy.strip().rstrip("/")
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
    return _bind_port(cleaned)


def live_vhosts(snapshot: dict) -> list[dict]:
    if not snapshot or not snapshot.get("ok"):
        return []
    nginx = snapshot.get("nginx") or {}
    rows = []
    for server in nginx.get("servers") or []:
        names = [n for n in (server.get("server_names") or []) if n and n != "_"]
        listens = server.get("listen") or []
        public_ports = sorted(
            {
                item.get("port")
                for item in listens
                if item.get("port")
                and item.get("addr") not in {"127.0.0.1", "::1"}
            }
        )
        if not public_ports:
            continue
        for loc in server.get("locations") or []:
            path = loc.get("path") or "/"
            if path.startswith("^~ "):
                path = path.split(" ", 1)[-1]
            if path in {".well-known/acme-challenge/", "/.well-known/acme-challenge/"}:
                continue
            if "well-known/acme-challenge" in path:
                continue
            proxy = loc.get("proxy_pass")
            if not proxy:
                continue
            rows.append(
                {
                    "names": names,
                    "ports": public_ports,
                    "path": path,
                    "proxy_pass": proxy,
                    "proxy_port": _proxy_port(proxy),
                    "ssl": any(item.get("ssl") for item in listens),
                }
            )
    return rows


def live_public_ports(snapshot: dict) -> list[int]:
    ports = set()
    for row in snapshot.get("ss") or []:
        if row.get("public") and row.get("port") not in {22, 2222, 53}:
            ports.add(int(row["port"]))
    return sorted(ports)


def live_ufw(snapshot: dict) -> list[str]:
    allows = (snapshot.get("ufw") or {}).get("allows") or []
    return list(dict.fromkeys(str(item) for item in allows))


def apply_live_to_hosts(hosts: dict[str, dict], snapshots: dict[str, dict]) -> None:
    for hid, host in hosts.items():
        snap = snapshots.get(hid)
        if not snap or not snap.get("ok"):
            host["live_ok"] = False
            if snap and snap.get("error"):
                host["live_error"] = snap["error"]
            continue
        host["live_ok"] = True
        host["live_at"] = snap.get("collected_at")
        if snap.get("hostname"):
            host["live_hostname"] = snap["hostname"]
        ports = live_public_ports(snap)
        if ports:
            host["public_listeners"] = [str(p) for p in ports]
            host["edge"] = (
                "nginx is the only public HTTP listener · "
                + " ".join(f":{p}" for p in ports)
            )
        allows = live_ufw(snap)
        if allows:
            host["ufw"] = allows
        notes = host.get("notes") or ""
        extra = []
        unexpected = [p for p in ports if p not in {80, 443}]
        if unexpected:
            extra.append("Unexpected public ports: " + ", ".join(f":{p}" for p in unexpected) + ".")
        if extra:
            host["notes"] = (notes + " " + " ".join(extra)).strip()


def reconcile(
    services: list[dict],
    hosts: dict[str, dict],
    snapshots: dict[str, dict],
) -> list[dict]:
    drift: list[dict] = []
    apply_live_to_hosts(hosts, snapshots)

    declared_by_host: dict[str, list[tuple[dict, dict, str]]] = {}
    for svc in services:
        hid = svc.get("host")
        if not hid:
            continue
        for route in svc.get("public") or []:
            key = _hostname(route)
            declared_by_host.setdefault(hid, []).append((svc, route, key))

    matched_declared: set[tuple[str, str]] = set()

    for hid, snap in snapshots.items():
        if not snap.get("ok"):
            if hid in hosts:
                drift.append(
                    {
                        "host": hid,
                        "kind": "collect_failed",
                        "detail": snap.get("error") or "live snapshot failed",
                    }
                )
            continue

        for port in live_public_ports(snap):
            if port not in {80, 443}:
                drift.append(
                    {
                        "host": hid,
                        "kind": "unexpected_public_port",
                        "detail": f"Host listens on :{port} (not 80/443)",
                    }
                )

        for row in snap.get("ss") or []:
            if row.get("public") and row.get("port") in {8765, 8787, 8090, 5050}:
                drift.append(
                    {
                        "host": hid,
                        "kind": "process_exposed",
                        "detail": f"App bind :{row['port']} is on {row.get('addr')}, not localhost",
                    }
                )

        vhosts = live_vhosts(snap)
        for vhost in vhosts:
            names = vhost["names"] or ["_"]
            path = vhost["path"]
            path_norm = "/" if path in {"/", "= /"} else path.lstrip("=").strip()
            if not path_norm.startswith("/"):
                path_norm = "/" + path_norm
            for name in names:
                live_key = name if path_norm in {"/", ""} else f"{name}{path_norm.rstrip('/')}"
                found = None
                for svc, route, key in declared_by_host.get(hid, []):
                    if key == live_key or key == name and path_norm in {"/", ""}:
                        found = (svc, route, key)
                        break
                    bind = _bind_port((svc.get("process") or {}).get("bind"))
                    if bind and bind == vhost.get("proxy_port") and (
                        key == name or key.split("/")[0] == name
                    ):
                        found = (svc, route, key)
                        break
                if found:
                    svc, route, key = found
                    route["live_match"] = True
                    route.setdefault("edge", "")
                    if not route.get("edge"):
                        ports = " ".join(f":{p}" for p in vhost["ports"])
                        route["edge"] = f"nginx {ports}"
                    matched_declared.add((hid, key))
                    continue
                if name == "_" :
                    continue
                if vhost.get("proxy_port") is None and path_norm in {"/", ""}:
                    continue
                same_process = any(
                    _bind_port((svc.get("process") or {}).get("bind")) == vhost.get("proxy_port")
                    for svc, _, _ in declared_by_host.get(hid, [])
                )
                if _is_ip(name) and same_process:
                    continue
                discovered = {
                    "repo": f"live:{hid}",
                    "name": "Live discovery",
                    "live": True,
                    "host": hid,
                    "source": f"live:{hid}:nginx",
                    "process": {
                        "name": "proxy",
                        "bind": vhost.get("proxy_pass") or "",
                    },
                    "store": "—",
                    "public": [
                        {
                            "name": live_key,
                            "url": f"https://{name}{path_norm if path_norm != '/' else '/'}",
                            "edge": "nginx (not in network.yaml)",
                            "probe": False,
                            "note": "Seen on the host; no matching network.yaml route",
                            "live_match": True,
                        }
                    ],
                    "notes": "Picked up from live nginx, not from a repo network.yaml.",
                    "discovered": True,
                }
                services.append(discovered)
                drift.append(
                    {
                        "host": hid,
                        "kind": "undeclared_route",
                        "detail": f"{live_key} -> {vhost.get('proxy_pass') or 'no proxy'} (not in network.yaml)",
                    }
                )

        for svc, route, key in declared_by_host.get(hid, []):
            if (hid, key) in matched_declared or route.get("live_match"):
                continue
            if route.get("probe") is False:
                continue
            host_only = key.split("/")[0]
            live_names = {
                n
                for vhost in vhosts
                for n in vhost["names"]
            }
            if host_only in live_names or key in live_names:
                route["live_match"] = True
                matched_declared.add((hid, key))
                continue
            route["live_match"] = False
            drift.append(
                {
                    "host": hid,
                    "kind": "declared_missing",
                    "detail": f"{svc.get('name') or svc.get('repo')}: {key} is in network.yaml but not in live nginx",
                }
            )

    return drift
