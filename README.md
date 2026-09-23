# SimbaServices org info

Private home for org-level facts. The remote-host schematic is generated from GitHub, not hand-edited.

## Remote host network

Open [`remote-host-network.html`](remote-host-network.html) in a browser (or the local copy at `C:\Simba\remote-host-network.html`).

It stays current by reading `network.yaml` from every **SimbaServices** repo, merging [`inventory/hosts.yaml`](inventory/hosts.yaml), **SSHing to each Ubuntu host** for live nginx/ufw/listener facts, and rewriting the HTML. Live routes that are not in yaml still appear. Declared routes that are gone from nginx are marked as drift.

```powershell
cd C:\Simba\Org_Info
python -m pip install -r tools/requirements.txt
python tools/generate_network.py --prefer-local --local-root C:\Simba --write-fallbacks --copy C:\Simba\remote-host-network.html
```

`gh` must be logged in (`repo` + `read:org`). The script lists the org, fetches each `network.yaml`, SSHs to the machines in `hosts.yaml` (BatchMode, host keys from that file), probes public health URLs, and does not fail the build if a probe or one host times out.

Live collection is the check against the boxes. Updating nginx/ufw without touching `network.yaml` still shows up on the next generate (or within 15 minutes on a host that has the snapshot timer). `--no-collect` reuses `inventory/live/*.json`.

## Add a new app to the map

1. In the new SimbaServices repo, add `network.yaml` at the root (or `deploy/network.yaml`).
2. Copy [`inventory/network.yaml.example`](inventory/network.yaml.example) and fill it in.
3. If the app uses a **new machine**, add that machine to `inventory/hosts.yaml` (SSH host key, public listeners, ufw). Existing machines: set `host: shared`, `host: wellnav`, or `host: propeval`.
4. Push the file. Refresh this repo:

```powershell
gh workflow run refresh-network.yml --repo SimbaServices/Org_Info
# or, from any repo:
gh api repos/SimbaServices/Org_Info/dispatches -f event_type=network-refresh
```

Do **not** put private keys, tokens, `.env` values, or passwords in `network.yaml`. Public SSH host keys and the login pubkey already used on the machines are fine. Process binds such as `127.0.0.1:8787` are localhost-only and must stay that way — they are documentation, not public listeners.

### `network.yaml` fields

| Field | Purpose |
|---|---|
| `schema` | `1` |
| `repo` | GitHub repo name |
| `name` | Display name |
| `live` | `true` if it answers on a public name; `false` for blueprints |
| `host` | Id from `inventory/hosts.yaml`, or an inline host mapping |
| `public` | Public names, edge, optional `health` URL |
| `process` | systemd/docker name and **localhost** bind |
| `store` | Database / volume |
| `edge_steps` | Optional flow for a single-app host card |
| `outbound` | Upstream hosts and what crosses |
| `notes` | Operator notes |

Repos without a network file still appear under **Org repos not on a remote host**.

Netlify (or any app that is not an org repo) can live under `inventory/services/*.yaml` with `org_extra: true`. The public site is `inventory/services/website.yaml`.

## Automation

[`.github/workflows/refresh-network.yml`](.github/workflows/refresh-network.yml) rebuilds daily and on `workflow_dispatch` / `repository_dispatch` (`network-refresh`). It commits `remote-host-network.html` only when the inventory actually changed (timestamps alone do not create a commit).

The default `GITHUB_TOKEN` can read this repo and public org repos. It cannot list or read other **private** org repos.

To collect live nginx/ufw from the Ubuntu hosts in Actions, add the login private key as **`SIMBA_SSH_KEY`**. Until that secret exists, the workflow uses the last `inventory/live/*.json` committed from a local generate. On each Ubuntu host, `tools/install-host-snapshot.sh` installs a 15-minute timer that writes `/var/lib/simba-network/live.json`. Deploy scripts (`restrict-govdeals-edge.sh`, `enable-https.sh`) refresh that file after nginx changes.

To pick up new private apps automatically, add a classic PAT (or fine-grained token) with **Contents: Read** on the SimbaServices org as the Actions secret **`ORG_READ_TOKEN`**. Until that secret exists, the workflow still draws current private apps from `inventory/services/*.yaml` fallbacks written by a local generate (`--write-fallbacks`), and it still fetches public repos (`GovDeals`, `Well_Navigation`) live.

A copy-paste workflow for service repos is [`inventory/notify-network.yml`](inventory/notify-network.yml). After you change `network.yaml` it dispatches Org_Info. That dispatch needs a token that can write `repository_dispatch` to this private repo; put it in the service repo as `ORG_INFO_DISPATCH_TOKEN` only if you want push-triggered refresh.

## Safety

- Shared host SSH is `root@95.216.155.122`. nginx listens on **80 and 443 only**.
- GovDeals public edge is **https://gd.simba.services/** only.
- CRM, GovDeals, and Maintenance bind `127.0.0.1` (`8787`, `8765`, `8090`). Do not expose those ports. Do not re-open 8080/8443.
