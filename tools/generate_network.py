#!/usr/bin/env python3
"""Build remote-host-network.html from SimbaServices network.yaml files."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from collect_live import collect as collect_live_hosts  # noqa: E402
from collect_live import load_saved as load_live_saved  # noqa: E402
from live_network import reconcile  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("PyYAML is required: pip install -r tools/requirements.txt\n")
    raise

ORG = "SimbaServices"
NETWORK_PATHS = ("network.yaml", "deploy/network.yaml")
SECRET_MARKERS = (
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "ghp_",
    "gho_",
    "github_pat_",
    "sk_live_",
    "sk_test_",
    "samsara_api_",
    "-----BEGIN",
)
STAMP_RE = re.compile(
    r"(Live HTTP probe )(\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)",
    re.I,
)


def e(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def looks_secret(value: object) -> bool:
    text = str(value)
    return any(marker in text for marker in SECRET_MARKERS)


def scrub(obj):
    if isinstance(obj, dict):
        return {
            key: "[redacted]" if looks_secret(val) else scrub(val)
            for key, val in obj.items()
        }
    if isinstance(obj, list):
        return [scrub(item) for item in obj]
    if looks_secret(obj):
        return "[redacted]"
    return obj


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must be a mapping")
    return scrub(data)


def run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def gh_json(args: list[str]):
    proc = run_gh(args)
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    if not text:
        return None
    return json.loads(text)


def list_org_repos() -> list[dict]:
    data = gh_json(
        [
            "repo",
            "list",
            ORG,
            "--limit",
            "200",
            "--json",
            "name,url,description,isPrivate,updatedAt,defaultBranchRef",
        ]
    )
    return data or []


def fetch_repo_file(repo: str, path: str, branch: str | None) -> dict | None:
    api = f"repos/{ORG}/{repo}/contents/{path}"
    if branch:
        api += f"?ref={branch}"
    payload = gh_json(["api", api])
    if not payload or payload.get("type") != "file" or not payload.get("content"):
        return None
    raw = base64.b64decode(payload["content"].encode("ascii"))
    data = yaml.safe_load(raw.decode("utf-8")) or {}
    if not isinstance(data, dict):
        return None
    return scrub(data)


def local_network_file(local_root: Path | None, folder: str) -> Path | None:
    if not local_root:
        return None
    base = local_root / folder
    for rel in NETWORK_PATHS:
        path = base / rel
        if path.is_file():
            return path
    return None


def merge_service(base: dict | None, incoming: dict | None) -> dict | None:
    if not incoming:
        return base
    if not base:
        return incoming
    merged = dict(base)
    merged.update(incoming)
    return merged


def probe_url(url: str, timeout: float) -> dict:
    result = {"url": url, "ok": False, "status": None, "detail": "not probed"}
    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "SimbaNetworkMap/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(240)
            result["ok"] = 200 <= resp.status < 400
            result["status"] = resp.status
            snippet = body.decode("utf-8", errors="replace").strip().replace("\n", " ")
            if snippet.startswith("{") and "ok" in snippet.lower():
                try:
                    parsed = json.loads(body.decode("utf-8", errors="replace"))
                    bits = [str(resp.status)]
                    if parsed.get("schema_version"):
                        bits.append(f"schema {parsed['schema_version']}")
                    if "users" in parsed:
                        bits.append(f"{parsed['users']} users")
                    result["detail"] = " · ".join(bits)
                except json.JSONDecodeError:
                    result["detail"] = f"{resp.status}"
            else:
                result["detail"] = f"{resp.status}"
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["detail"] = str(exc.code)
    except Exception as exc:  # noqa: BLE001 — probe must never fail the build
        result["detail"] = type(exc).__name__
    return result


def as_host_id(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return str(value)


def host_index(hosts_cfg: dict) -> dict[str, dict]:
    out = {}
    for host in hosts_cfg.get("hosts") or []:
        if host.get("id"):
            out[host["id"]] = dict(host)
    return out


def ensure_inline_host(hosts: dict[str, dict], spec) -> str | None:
    if not isinstance(spec, dict):
        return as_host_id(spec)
    hid = spec.get("id")
    if not hid:
        return None
    if hid not in hosts:
        incoming = dict(spec)
        incoming.setdefault("kind", "ubuntu")
        incoming.setdefault("order", 80)
        incoming.setdefault("label", hid)
        hosts[hid] = incoming
    return hid


def collect_services(
    hosts_cfg: dict,
    repos: list[dict],
    *,
    local_root: Path | None,
    prefer_local: bool,
    write_fallbacks: bool,
) -> tuple[list[dict], list[dict], dict[str, dict]]:
    hosts = host_index(hosts_cfg)
    local_map = hosts_cfg.get("local_roots") or {}
    fallback_dir = ROOT / "inventory" / "services"
    fallback_dir.mkdir(parents=True, exist_ok=True)

    services: list[dict] = []
    missing: list[dict] = []
    seen_repos: set[str] = set()

    for repo in repos:
        name = repo["name"]
        seen_repos.add(name)
        branch = ((repo.get("defaultBranchRef") or {}).get("name") or "").strip() or None
        data = None
        source = None

        local_path = local_network_file(local_root, local_map.get(name, name))
        if prefer_local and local_path:
            data = load_yaml(local_path)
            source = f"local:{local_path}"

        if data is None and branch:
            for rel in NETWORK_PATHS:
                fetched = fetch_repo_file(name, rel, branch)
                if fetched:
                    data = fetched
                    source = f"github:{name}/{rel}"
                    break

        if data is None and local_path:
            data = load_yaml(local_path)
            source = f"local:{local_path}"

        fallback = fallback_dir / f"{name}.yaml"
        if data is None and fallback.is_file():
            data = load_yaml(fallback)
            source = f"fallback:{fallback.name}"

        if data is None:
            if name not in (hosts_cfg.get("exclude_from_missing") or []):
                missing.append(
                    {
                        "name": name,
                        "url": repo.get("url"),
                        "description": repo.get("description") or "",
                        "private": bool(repo.get("isPrivate")),
                    }
                )
            continue

        hid = ensure_inline_host(hosts, data.get("host"))
        data = dict(data)
        data["host"] = hid
        data["repo"] = data.get("repo") or name
        data["github"] = repo.get("url")
        data["source"] = source
        data["private"] = bool(repo.get("isPrivate"))
        services.append(data)

        if write_fallbacks and data.get("private") and not data.get("org_extra"):
            fallback.write_text(
                yaml.safe_dump(
                    {k: v for k, v in data.items() if k not in {"source", "github", "private"}},
                    sort_keys=False,
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

    for extra in sorted(fallback_dir.glob("*.yaml")):
        data = load_yaml(extra)
        repo_name = data.get("repo") or extra.stem
        if repo_name in seen_repos and any(s.get("repo") == repo_name for s in services):
            continue
        if not data.get("org_extra") and repo_name in seen_repos:
            continue
        hid = ensure_inline_host(hosts, data.get("host"))
        data = dict(data)
        data["host"] = hid
        data["repo"] = repo_name
        data["source"] = f"org:{extra.name}"
        services.append(data)

    return services, missing, hosts


def probe_services(services: list[dict], timeout: float) -> None:
    cache: dict[str, dict] = {}
    for svc in services:
        for route in svc.get("public") or []:
            url = route.get("health") or route.get("url")
            if not url or route.get("probe") is False:
                route["probe_result"] = {
                    "ok": None,
                    "detail": route.get("note") or "not probed",
                }
                continue
            if url not in cache:
                cache[url] = probe_url(url, timeout)
            route["probe_result"] = cache[url]


def route_rows(services: list[dict]) -> list[list[str]]:
    rows = []
    for svc in services:
        if not svc.get("live", True):
            continue
        hid = svc.get("host") or ""
        process = svc.get("process") or {}
        bind = process.get("bind")
        pname = process.get("name") or ""
        process_cell = " ".join(part for part in (pname, bind) if part) or "—"
        store = svc.get("store") or "—"
        for route in svc.get("public") or []:
            probe = (route.get("probe_result") or {}).get("detail") or "—"
            if (route.get("probe_result") or {}).get("ok"):
                from urllib.parse import urlparse

                health = route.get("health") or route.get("url") or ""
                health_path = urlparse(health).path or "/" if health.startswith("http") else ""
                extra = probe[3:].strip(" ·") if str(probe).startswith("200") else ""
                probe = " · ".join(part for part in (f"200 {health_path}".strip(), extra) if part)
            rows.append(
                [
                    hid,
                    svc.get("name") or svc.get("repo") or "",
                    route.get("name") or "",
                    route.get("edge") or "",
                    process_cell,
                    store,
                    probe,
                ]
            )
    return rows


def ssh_rows(hosts: dict[str, dict], login_key: dict | None) -> list[list[str]]:
    rows = []
    for host in sorted(hosts.values(), key=lambda h: h.get("order", 50)):
        if host.get("edge_only") or host.get("ssh_hidden") or host.get("kind") == "netlify":
            continue
        ssh = host.get("ssh") or {}
        if not ssh:
            continue
        hid = host["id"]
        label = host.get("label") or hid
        address = ssh.get("address") or host.get("ipv4") or ""
        port = ssh.get("port") or 22
        user = ssh.get("user")
        host_key = ssh.get("host_key")
        if host_key:
            addr = f"{address}:{port}" if port == 22 else f"[{address}]:{port}"
            rows.append([hid, label, addr, "Host ed25519", host_key])
        if login_key and login_key.get("key"):
            login_addr = f"{user}@{address}:{port}" if user else f"{address}:{port}"
            rows.append(
                [
                    hid,
                    label,
                    login_addr,
                    login_key.get("label") or "Login key",
                    login_key["key"],
                ]
            )
        for extra in host.get("extra_keys") or []:
            rows.append(
                [
                    hid,
                    label,
                    extra.get("target") or "github.com from this host",
                    extra.get("label") or extra.get("file") or "Deploy key",
                    extra.get("key") or "",
                ]
            )
    return rows


def outbound_rows(services: list[dict]) -> list[list[str]]:
    rows = []
    for svc in services:
        if not svc.get("live", True):
            continue
        hid = svc.get("host") or ""
        for item in svc.get("outbound") or []:
            rows.append(
                [
                    hid,
                    svc.get("name") or svc.get("repo") or "",
                    item.get("to") or "",
                    item.get("what") or "",
                ]
            )
    return rows


def live_apps(services: list[dict]) -> list[dict]:
    return [s for s in services if s.get("live", True) and (s.get("public") or s.get("host"))]


def stats(services: list[dict], hosts: dict[str, dict]) -> dict:
    ubuntu = [
        h
        for h in hosts.values()
        if h.get("kind") == "ubuntu"
        and any(s.get("host") == h["id"] and s.get("live", True) for s in services)
    ]
    ok_apps = 0
    for svc in live_apps(services):
        host = hosts.get(svc.get("host") or "")
        if host and (host.get("edge_only") or host.get("kind") == "netlify"):
            continue
        routes = [r for r in (svc.get("public") or []) if r.get("probe") is not False]
        probed = [r for r in routes if r.get("health") or r.get("url")]
        if probed and all((r.get("probe_result") or {}).get("ok") for r in probed):
            ok_apps += 1
        elif not probed:
            ok_apps += 1
    shared = sum(1 for h in ubuntu if h.get("shared"))
    return {
        "hosts": len(ubuntu),
        "ok_apps": ok_apps,
        "shared": shared,
    }


def js_rows(rows: list[list[str]]) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2)


def host_card(host: dict, apps: list[dict]) -> str:
    hid = host["id"]
    shared = bool(host.get("shared"))
    title = host.get("label") or hid
    if shared and len(apps) > 1:
        title = f"{title} — {len(apps)} apps"
    ips = " · ".join(p for p in (host.get("ipv4"), host.get("ipv6")) if p)
    meta = host.get("meta") or ""
    note_bits = []
    if host.get("notes"):
        note_bits.append(str(host["notes"]).strip())
    ufw = host.get("ufw")
    if ufw:
        note_bits.append("ufw allows " + ", ".join(str(x) for x in ufw) + ".")
    if not shared:
        for app in apps:
            if app.get("notes"):
                text = str(app["notes"]).strip()
                if text and text not in note_bits:
                    note_bits.append(text)
    apps = sorted(apps, key=lambda a: (a.get("order", 50), a.get("name") or ""))
    keys = []
    ssh = host.get("ssh") or {}
    if ssh.get("host_key"):
        keys.append(f"<b>Host key, port {e(ssh.get('port') or 22)}</b>{e(ssh['host_key'])}")
    login = host.get("_login_key")
    if login:
        keys.append(f"<b>{e(login.get('label') or 'Login key')}</b>{e(login['key'])}")
    for extra in host.get("extra_keys") or []:
        keys.append(f"<b>{e(extra.get('label') or 'Key')}</b>{e(extra.get('key') or '')}")

    if shared or len(apps) > 1:
        bus = host.get("edge") or "nginx is the only public HTTP listener"
        svcs = []
        for app in apps:
            process = app.get("process") or {}
            public_names = ", ".join(
                r.get("name") or "" for r in (app.get("public") or []) if r.get("name")
            )
            svcs.append(
                "<div class=\"svc\">"
                f"<div><strong>{e(app.get('name'))}</strong><em>{e(public_names)}</em></div>"
                "<span class=\"to\">→</span>"
                f"<div><strong>{e(process.get('bind') or '—')}</strong><em>{e(process.get('name') or '')}</em></div>"
                "<span class=\"to\">→</span>"
                f"<div><strong>{e((app.get('store') or '—').split(' ')[0])}</strong><em>{e(app.get('store') or '')}</em></div>"
                "</div>"
            )
        body = (
            f"<div class=\"bus\">{e(bus)}</div>"
            f"<div class=\"services\">{''.join(svcs)}</div>"
        )
    else:
        steps = []
        app = apps[0] if apps else {}
        flow = app.get("edge_steps") or []
        if not flow and app:
            process = app.get("process") or {}
            flow = [
                {"title": "edge", "detail": ", ".join(r.get("name") or "" for r in (app.get("public") or []))},
                {"title": process.get("name") or "process", "detail": process.get("bind") or ""},
            ]
            if app.get("store"):
                flow.append({"title": "store", "detail": app["store"]})
        for i, step in enumerate(flow):
            if i:
                steps.append('<div class="arrow">↓</div>')
            steps.append(
                f"<div class=\"step\"><strong>{e(step.get('title'))}</strong>"
                f"<em>{e(step.get('detail'))}</em></div>"
            )
        body = f"<div class=\"flow\">{''.join(steps)}</div>"

    return (
        f"<section class=\"host{' shared' if shared else ''}\" data-part=\"{e(hid)}\">"
        f"<h3>{e(title)}</h3>"
        f"<div class=\"ip\">{e(ips)}</div>"
        f"<div class=\"meta\">{e(meta)}</div>"
        f"{body}"
        f"<p class=\"note\">{e(' '.join(note_bits))}</p>"
        f"<p class=\"key\">{''.join(keys)}</p>"
        "</section>"
    )


def outbound_cards(services: list[dict], mail: dict | None) -> str:
    groups: dict[str, dict] = {}
    for svc in services:
        if not svc.get("live", True):
            continue
        hid = svc.get("host") or ""
        for item in svc.get("outbound") or []:
            dest = item.get("to") or ""
            bucket = groups.setdefault(dest, {"hosts": [], "bits": [], "from": []})
            if hid and hid not in bucket["hosts"]:
                bucket["hosts"].append(hid)
            if svc.get("name") and svc["name"] not in bucket["from"]:
                bucket["from"].append(svc["name"])
            if item.get("what"):
                bucket["bits"].append(item["what"])
    if mail and mail.get("host"):
        dest = mail["host"]
        if dest not in groups:
            groups[dest] = {
                "hosts": [],
                "from": [],
                "bits": [mail.get("notes") or ""],
            }
        elif mail.get("notes"):
            groups[dest]["bits"].append(mail["notes"])
    cards = []
    for dest, info in groups.items():
        parts = " ".join(info["hosts"]) or "all"
        who = ", ".join(info["from"])
        detail = " ".join(dict.fromkeys(info["bits"]))
        if who and dest != (mail or {}).get("host"):
            text = f"{who}. {detail}".strip()
        else:
            text = detail or who
        cards.append(
            f"<article class=\"out\" data-part=\"{e(parts)}\">"
            f"<strong>{e(dest)}</strong>"
            f"<span>{e(text)}</span>"
            "</article>"
        )
    return "\n      ".join(cards)


def render_html(
    *,
    services: list[dict],
    missing: list[dict],
    hosts: dict[str, dict],
    hosts_cfg: dict,
    generated_at: str,
    drift: list[dict] | None = None,
) -> str:
    login_key = hosts_cfg.get("login_key")
    for host in hosts.values():
        if not host.get("edge_only"):
            host["_login_key"] = login_key

    ubuntu_hosts = [
        h
        for h in hosts.values()
        if h.get("kind") != "netlify" and not h.get("edge_only")
        and any(s.get("host") == h["id"] and s.get("live", True) for s in services)
    ]
    ubuntu_hosts.sort(key=lambda h: h.get("order", 50))
    edge_hosts = [
        h
        for h in hosts.values()
        if h.get("edge_only") or h.get("kind") == "netlify"
        and any(s.get("host") == h["id"] for s in services)
    ]
    edge_hosts.sort(key=lambda h: h.get("order", 50))

    s = stats(services, hosts)
    filters = ['<button type="button" data-focus="all" aria-pressed="true">All hosts</button>']
    for host in ubuntu_hosts + edge_hosts:
        label = host.get("filter_label") or host.get("label") or host["id"]
        filters.append(
            f'<button type="button" data-focus="{e(host["id"])}" aria-pressed="false">{e(label)}</button>'
        )

    cf = hosts_cfg.get("cloudflare") or {}
    cf_ips = " · ".join(cf.get("ipv4") or [])
    cf_names = ", ".join(cf.get("names") or [])
    edge_bars = [
        "<div class=\"bar\">"
        f"<strong>{e(cf.get('label') or 'Cloudflare proxy')}</strong>"
        f"<span>{e(cf_ips)} · {e(cf_names)}</span>"
        "</div>"
    ]
    for host in edge_hosts:
        apps = [svc for svc in services if svc.get("host") == host["id"] and svc.get("live", True)]
        names = ", ".join(
            r.get("name") or ""
            for app in apps
            for r in (app.get("public") or [])
        )
        edge_bars.append(
            f"<div class=\"bar\" data-part=\"{e(host['id'])}\">"
            f"<strong>{e(host.get('label') or host['id'])} · Netlify</strong>"
            f"<span>{e(host.get('meta') or names)}</span>"
            "</div>"
        )

    cards = []
    for host in ubuntu_hosts:
        apps = [svc for svc in services if svc.get("host") == host["id"] and svc.get("live", True)]
        cards.append(host_card(host, apps))

    host_grid = "250px 1fr 250px" if len(ubuntu_hosts) == 3 else "repeat(auto-fit, minmax(250px, 1fr))"
    edge_grid = "1fr 280px" if edge_hosts else "1fr"

    not_live = [svc for svc in services if not svc.get("live", True)]
    aside_bits = []
    if missing:
        names = ", ".join(m["name"] for m in missing)
        aside_bits.append(
            f"{names} have no network.yaml (or deploy/network.yaml) on the default branch."
        )
    for svc in not_live:
        note = (svc.get("notes") or "").strip()
        aside_bits.append(f"{svc.get('name') or svc.get('repo')}: {note}" if note else f"{svc.get('name')} is not live.")
    if not aside_bits:
        aside_bits.append("Every SimbaServices repo that published a network file is on the map.")

    routes = route_rows(services)
    ssh = ssh_rows(hosts, login_key)
    outbound = outbound_rows(services)
    drift = drift or []
    drift_rows = [
        [
            item.get("host") or "",
            item.get("host") or "",
            item.get("kind") or "",
            item.get("detail") or "",
        ]
        for item in drift
    ]
    ok_probes = sum(1 for row in routes if str(row[-1]).startswith("200"))
    live_hosts = sum(1 for h in hosts.values() if h.get("live_ok"))
    caption = (
        f"Live HTTP probe {generated_at}. {ok_probes} public route(s) returned 200. "
        f"Live nginx/ufw/listeners collected from {live_hosts} Ubuntu host(s). "
        "Declared network.yaml routes are reconciled against that snapshot so host "
        "changes are not missed. App bind addresses stay on localhost."
    )

    lead = (
        f"{s['hosts']} Ubuntu machine(s) plus any Netlify edge. "
        "Every *.simba.services name below is intended to answer through Cloudflare "
        "unless the row says otherwise."
    )

    site_ssh = next((h.get("notes") for h in edge_hosts if h.get("notes")), "No SSH host for this edge.")
    site_out = next(
        (h.get("outbound_empty") for h in edge_hosts if h.get("outbound_empty")),
        "No outbound rows for this host.",
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Simba remote host network</title>
  <style>
    :root {{
      --bg: #101114;
      --surface: #181b20;
      --chip: #22262d;
      --line: #3a404a;
      --text: #eceff3;
      --muted: #a7b0bb;
      --faint: #7d8793;
      --accent: #7aa2ff;
      --ok: #3ecf8e;
      --warn: #e2b340;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.45 "Segoe UI", system-ui, sans-serif;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 20px 64px;
    }}
    h1 {{ font-size: 24px; font-weight: 600; margin: 0 0 6px; }}
    h2 {{ font-size: 18px; font-weight: 600; margin: 28px 0 10px; }}
    h3 {{ font-size: 14px; font-weight: 600; margin: 0 0 8px; }}
    p {{ margin: 0; color: var(--muted); }}
    .lead {{ max-width: 68ch; }}
    .stats {{
      display: flex;
      gap: 28px;
      margin: 18px 0 16px;
    }}
    .stat b {{ display: block; font-size: 22px; font-weight: 600; color: var(--text); }}
    .stat span {{ color: var(--faint); font-size: 12px; }}
    .stat.ok b {{ color: var(--ok); }}
    .stat.warn b {{ color: var(--warn); }}
    .filters {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
    button {{
      font: inherit;
      font-size: 13px;
      color: var(--text);
      background: var(--chip);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 10px;
      cursor: pointer;
    }}
    button[aria-pressed="true"] {{
      background: var(--accent);
      border-color: var(--accent);
      color: #101114;
    }}
    .schematic {{ display: flex; flex-direction: column; gap: 0; }}
    .bar, .host, .out, .step, .svc {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .bar {{ padding: 10px 14px; }}
    .bar strong {{ display: block; font-size: 13px; }}
    .bar span, .host .meta, .step em, .svc em, .out span {{ color: var(--muted); font-size: 12px; font-style: normal; }}
    .edge {{
      display: grid;
      grid-template-columns: {edge_grid};
      gap: 12px;
      margin-top: 14px;
    }}
    .hosts {{
      display: grid;
      grid-template-columns: {host_grid};
      gap: 12px;
      margin-top: 14px;
    }}
    .host {{ padding: 12px 14px 14px; min-height: 100%; }}
    .host.shared {{ border-color: var(--accent); }}
    .host h3 {{ font-size: 14px; }}
    .host .ip {{ color: var(--muted); font-size: 13px; margin-top: 2px; }}
    .host .meta {{ margin: 2px 0 10px; }}
    .bus {{
      background: var(--chip);
      border-radius: 4px;
      padding: 6px 8px;
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .flow, .services {{ display: flex; flex-direction: column; gap: 6px; }}
    .step, .svc {{ background: var(--chip); border-radius: 4px; padding: 7px 8px; }}
    .step strong, .svc strong {{ display: block; font-size: 12px; font-weight: 600; }}
    .arrow {{ color: var(--faint); font-size: 12px; padding-left: 8px; }}
    .svc {{
      display: grid;
      grid-template-columns: 1.3fr auto 1fr auto 0.8fr;
      gap: 8px;
      align-items: center;
    }}
    .svc .to {{ color: var(--faint); }}
    .note {{ color: var(--faint); font-size: 11px; margin-top: 10px; }}
    .key {{
      margin: 8px 0 0;
      font-family: Consolas, ui-monospace, monospace;
      font-size: 11px;
      line-height: 1.35;
      word-break: break-all;
      color: var(--muted);
    }}
    .key b {{
      display: block;
      margin-top: 6px;
      color: var(--text);
      font-family: "Segoe UI", system-ui, sans-serif;
      font-size: 11px;
    }}
    td.keycell {{
      font-family: Consolas, ui-monospace, monospace;
      font-size: 11px;
      word-break: break-all;
    }}
    .outs {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px;
      margin-top: 14px;
    }}
    .out {{ padding: 10px 12px; }}
    .out strong {{ display: block; font-size: 13px; margin-bottom: 4px; }}
    .caption {{ color: var(--faint); font-size: 12px; margin-top: 10px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ color: var(--faint); font-weight: 600; font-size: 12px; }}
    tr:nth-child(even) td {{ background: #14171b; }}
    td[data-ok="1"] {{ color: var(--ok); }}
    td[data-warn="1"] {{ color: var(--warn); }}
    .aside {{
      margin-top: 22px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      color: var(--muted);
      font-size: 14px;
    }}
    .aside strong {{ color: var(--text); display: block; margin-bottom: 4px; }}
    [data-part].dim {{ opacity: 0.28; }}
    @media (max-width: 980px) {{
      .edge, .hosts, .outs, .svc {{ grid-template-columns: 1fr; }}
      .svc .to {{ display: none; }}
    }}
    @media print {{
      body {{ background: #fff; color: #111; }}
      .bar, .host, .out, .step, .svc, .aside {{ border-color: #ccc; background: #fff; }}
      button {{ display: none; }}
    }}
  </style>
</head>
<body>
<main>
  <h1>Remote host network</h1>
  <p class="lead">{e(lead)}</p>

  <div class="stats">
    <div class="stat"><b>{s['hosts']}</b><span>Ubuntu hosts</span></div>
    <div class="stat ok"><b>{s['ok_apps']}</b><span>Apps returning 200</span></div>
    <div class="stat"><b>{s['shared']}</b><span>Shared machine</span></div>
    <div class="stat{' warn' if drift else ''}"><b>{len(drift)}</b><span>Live vs declared drift</span></div>
  </div>

  <div class="filters" role="group" aria-label="Highlight a host">
    {''.join(filters)}
  </div>

  <div class="schematic" id="schematic">
    <div class="bar">
      <strong>{e((hosts_cfg.get('clients') or {}).get('label') or 'Clients')}</strong>
      <span>{e((hosts_cfg.get('clients') or {}).get('detail') or '')}</span>
    </div>

    <div class="edge">
      {''.join(edge_bars)}
    </div>

    <div class="hosts">
      {''.join(cards)}
    </div>

    <div class="outs">
      {outbound_cards(services, hosts_cfg.get('mail'))}
    </div>
  </div>

  <p class="caption">{e(caption)}</p>

  <h2>Public routes</h2>
  <table id="routes">
    <thead>
      <tr>
        <th>Project</th><th>Public name</th><th>Edge</th><th>Process</th><th>Store</th><th>Probe</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>

  <h2>Live host vs declared</h2>
  <p class="caption">nginx server_names, public listeners, and ufw on each Ubuntu host, compared with network.yaml. Empty means the schematic matches the box.</p>
  <table id="drift">
    <thead>
      <tr><th>Host</th><th>Kind</th><th>Detail</th></tr>
    </thead>
    <tbody></tbody>
  </table>
  <p id="drift-empty" hidden>No drift. Live nginx/ufw/listeners match the declared inventory.</p>

  <h2>SSH</h2>
  <table id="ssh">
    <thead>
      <tr><th>Machine</th><th>Address</th><th>Key</th><th>Public key</th></tr>
    </thead>
    <tbody></tbody>
  </table>
  <p id="ssh-site" hidden>{e(site_ssh)}</p>

  <h2>Outbound from the running processes</h2>
  <table id="outbound">
    <thead>
      <tr><th>From</th><th>To</th><th>What crosses</th></tr>
    </thead>
    <tbody></tbody>
  </table>
  <p id="outbound-site" hidden>{e(site_out)}</p>

  <div class="aside">
    <strong>Org repos not on a remote host</strong>
    {e(' '.join(aside_bits))} Add a network.yaml at the repo root (or deploy/network.yaml) to appear on this map.
  </div>
</main>
<script>
  const routes = {js_rows(routes)};
  const ssh = {js_rows(ssh)};
  const outbound = {js_rows(outbound)};
  const drift = {js_rows(drift_rows)};

  function fill(table, rows, focus) {{
    const body = table.querySelector("tbody");
    body.replaceChildren();
    rows.filter((row) => focus === "all" || row[0] === focus).forEach((row) => {{
      const tr = document.createElement("tr");
      row.slice(1).forEach((cell) => {{
        const td = document.createElement("td");
        td.textContent = cell;
        if (String(cell).startsWith("ssh-")) td.className = "keycell";
        if (String(cell).startsWith("200")) td.dataset.ok = "1";
        if (["undeclared_route", "declared_missing", "unexpected_public_port", "process_exposed"].includes(String(cell))) td.dataset.warn = "1";
        tr.append(td);
      }});
      body.append(tr);
    }});
    return body.children.length;
  }}

  function apply(focus) {{
    document.querySelectorAll("[data-part]").forEach((node) => {{
      const parts = node.getAttribute("data-part").split(" ");
      node.classList.toggle("dim", focus !== "all" && !parts.includes(focus));
    }});
    document.querySelectorAll(".filters button").forEach((button) => {{
      button.setAttribute("aria-pressed", button.dataset.focus === focus ? "true" : "false");
    }});
    fill(document.querySelector("#routes"), routes, focus);
    const driftCount = fill(document.querySelector("#drift"), drift, focus);
    document.querySelector("#drift").hidden = driftCount === 0;
    document.querySelector("#drift-empty").hidden = driftCount !== 0;
    const sshCount = fill(document.querySelector("#ssh"), ssh, focus);
    document.querySelector("#ssh").hidden = focus === "site" || sshCount === 0;
    document.querySelector("#ssh-site").hidden = !(focus === "site" || sshCount === 0);
    if (focus !== "site" && sshCount > 0) document.querySelector("#ssh-site").hidden = true;
    const outCount = fill(document.querySelector("#outbound"), outbound, focus);
    document.querySelector("#outbound").hidden = outCount === 0;
    document.querySelector("#outbound-site").hidden = outCount !== 0;
    void sshCount;
  }}

  document.querySelector(".filters").addEventListener("click", (event) => {{
    const button = event.target.closest("button");
    if (!button) return;
    apply(button.dataset.focus);
  }});
  apply("all");
</script>
</body>
</html>
"""


def normalize_html(text: str) -> str:
    return STAMP_RE.sub(r"\1TIMESTAMP", text)


def write_if_changed(path: Path, text: str) -> bool:
    if path.is_file() and normalize_html(path.read_text(encoding="utf-8")) == normalize_html(text):
        return False
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def snapshot(services: list[dict], missing: list[dict], generated_at: str, drift: list[dict] | None = None) -> dict:
    slim = []
    for svc in services:
        slim.append(
            {
                "repo": svc.get("repo"),
                "name": svc.get("name"),
                "live": svc.get("live", True),
                "host": svc.get("host"),
                "source": svc.get("source"),
                "public": [
                    {
                        "name": r.get("name"),
                        "url": r.get("url"),
                        "edge": r.get("edge"),
                        "probe": (r.get("probe_result") or {}).get("detail"),
                    }
                    for r in (svc.get("public") or [])
                ],
            }
        )
    return {
        "generated_at": generated_at,
        "services": slim,
        "missing": missing,
        "drift": drift or [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "remote-host-network.html")
    parser.add_argument("--copy", type=Path, action="append", default=[])
    parser.add_argument("--local-root", type=Path, default=None)
    parser.add_argument("--prefer-local", action="store_true")
    parser.add_argument("--write-fallbacks", action="store_true")
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--no-collect", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    hosts_cfg = load_yaml(ROOT / "inventory" / "hosts.yaml")
    repos = list_org_repos()
    if not repos:
        sys.stderr.write("warning: gh repo list returned nothing; using local/fallback files only\n")

    services, missing, hosts = collect_services(
        hosts_cfg,
        repos,
        local_root=args.local_root,
        prefer_local=args.prefer_local,
        write_fallbacks=args.write_fallbacks,
    )
    snapshots = {}
    if not args.no_collect:
        try:
            snapshots = collect_live_hosts(hosts_cfg.get("hosts") or [], timeout=int(args.timeout))
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"warning: live collect failed ({exc}); using saved snapshots\n")
            snapshots = load_live_saved()
    else:
        snapshots = load_live_saved()
    drift = reconcile(services, hosts, snapshots)
    if not args.no_probe:
        probe_services(services, args.timeout)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_text = render_html(
        services=services,
        missing=missing,
        hosts=hosts,
        hosts_cfg=hosts_cfg,
        generated_at=generated_at,
        drift=drift,
    )
    changed = write_if_changed(args.out, html_text)
    for dest in args.copy:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(args.out.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")

    snap_path = ROOT / "inventory" / "generated.json"
    snap_path.write_text(
        json.dumps(snapshot(services, missing, generated_at, drift), indent=2) + "\n",
        encoding="utf-8",
    )

    live = [s.get("name") or s.get("repo") for s in services if s.get("live", True)]
    print(f"services: {len(services)} live={live}")
    print(f"missing network.yaml: {[m['name'] for m in missing]}")
    print(f"drift: {len(drift)}")
    for item in drift:
        print(f"  {item.get('kind')}: {item.get('detail')}".encode("ascii", "replace").decode("ascii"))
    print(f"wrote {args.out} changed={changed}")
    for dest in args.copy:
        print(f"copied {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
