# AlphaLens Free Hosting Options

Two specs for hosting AlphaLens at $0/month, written 2026-11-19.

**Context that shapes both specs:**

- The dashboard has **no authentication** and exposes holdings, an EasyEquities
  sync button, and a backup button. The localhost binding + firewall hardening
  (see `AGENTS.md`) exists specifically because of this — any hosted option must
  replace it, not just move it.
- The app is stateful: SQLite or Postgres, `data/backups/`, `data/logs/`.
- `run_updater.py` daemon mode already contains the full schedule
  (09:00/13:00/17:00 price+news, 18:00 full, daily 08:00 IPO scan, 60s loop
  covering `_backup_if_due` and ~daily EE sync). `_backup_if_due` runs **only**
  inside the daemon loop; EE sync fires inside `_snapshot_portfolio` after every
  price run, so `--price-news` one-shots cover it.
- Portability already verified: `process_lock.py` handles `fcntl` and `msvcrt`;
  `pywin32` is Windows-gated in `requirements.txt`; all other deps have
  aarch64 + Python 3.13 wheels; `app/models/base.py` already sets
  `pool_pre_ping=True` and `-c timezone=UTC` for Postgres.
- Current DB size ~8.3 MB; dumps ~2.5 MB.
- Highest-risk secret: the **EasyEquities password** (full brokerage login).
  Luno is already a read-only key.

---

## Option A — Oracle Cloud Always Free VM

### A.1 Architecture

```
┌──────────────────────── OCI tenancy (home region) ────────────────────────┐
│  VCN 10.0.0.0/16                                                          │
│  └─ VM.Standard.A1.Flex (Ampere ARM, 2 OCPU / 12 GB / ~100 GB boot)       │
│     Ubuntu 24.04 aarch64, public IP                                       │
│                                                                           │
│     ├─ systemd: alphalens-updater.service  → run_updater.py (daemon mode) │
│     ├─ systemd: alphalens-dashboard.service → streamlit run main.py       │
│     │        bound to 127.0.0.1:8501  (never public)                      │
│     ├─ SQLite at /opt/alphalens/app/data/stock_analyzer.db                │
│     ├─ cron → rclone → OCI Object Storage (off-box backups, 20 GB free)   │
│     └─ tailscale0 (optional) for dashboard access from phone/laptop       │
└───────────────────────────────────────────────────────────────────────────┘
        Access: ssh -L 8501:localhost:8501  (occasional)
                or Tailscale IP:8501        (convenient)
```

Single service user `alphalens` owns the code, venv, `.env`, and `data/`.

### A.2 OCI account decisions (permanent — choose carefully)

- **Home region is chosen at signup and cannot be changed**, and Always Free
  instances must be created *in* the home region. Pick a region where A1
  shapes exist and have capacity. Johannesburg (`af-jnb`) is closest to
  JSE/EasyEquities; Frankfurt/Amsterdam commonly have capacity.
- **Free limits:** an Always Free (never-upgraded) tenancy gets ~2 OCPU /
  12 GB ARM. Upgrading to PAYG grows the A1 allowance to 4 OCPU / 24 GB *and
  stays free* — upgrading also fixes most "out of host capacity" pain. Spec
  assumes 2 OCPU / 12 GB, which is plenty.
- **"Out of host capacity"** on A1 is normal in busy regions. Mitigations:
  try every availability domain, retry at off-peak hours, script the retry
  loop, or upgrade to PAYG.
- Set a **budget alert at $1** immediately — the free tier still requires a
  card on file.
- Enable **MFA** on the OCI account. This VM will hold the EE password.

### A.3 VM provisioning

- **Shape:** `VM.Standard.A1.Flex` — 2 OCPU, 12 GB RAM.
- **Image:** Ubuntu 24.04 (aarch64).
- **Boot volume:** ~100 GB (the 200 GB free block-storage allowance is
  shared; leave headroom for volume backups or a second volume).
- **Networking:** public subnet, public IPv4.
- **Security list / NSG ingress:**
  - `tcp/22` from your home IP only (or `0.0.0.0/0` temporarily if the IP is
    dynamic — tighten later).
  - **Nothing else.** No 8501, no 80, no 443.
- **Egress:** unrestricted (Yahoo, Moneyweb, EE, Luno, SENS, RSS).

### A.4 OS setup

```bash
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone Africa/Johannesburg   # matches SCHEDULER_TIMEZONE
sudo apt install -y git curl ufw

# service user, no login shell
sudo useradd -r -m -d /opt/alphalens -s /usr/sbin/nologin alphalens

# Python 3.13 via uv (deadsnakes arm64 is spotty; uv installs standalone CPython)
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo -u alphalens bash -c '
  cd /opt/alphalens
  git clone <repo-url> app && cd app
  uv python install 3.13
  uv venv .venv --python 3.13
  uv pip install -r requirements.txt
'
```

Dependency notes: pandas/numpy/scipy/lxml/yfinance all have aarch64+3.13
wheels. `pywin32` is skipped by its environment marker. `tzdata` is harmless
on Linux. `psycopg2-binary` installs but stays unused if you keep SQLite
(recommended — see A.8).

Copy in `data/stock_analyzer.db`, `data/backups/`, and a real `.env` via
`scp`/rsync. `.env` needs **no path changes** — everything is relative
(`./data/...`). `chmod 600 .env`, owned by `alphalens`.

### A.5 systemd units (replaces Task Scheduler + windows_service.py)

`/etc/systemd/system/alphalens-updater.service` — daemon mode already owns
the full schedule, the 60s backup loop, the daily EE sync, and missed-run
catch-up. No cron timers needed; `--logon` is redundant since the daemon
survives reboot via the unit:

```ini
[Unit]
Description=AlphaLens headless updater
After=network-online.target
Wants=network-online.target

[Service]
User=alphalens
WorkingDirectory=/opt/alphalens/app
ExecStart=/opt/alphalens/app/.venv/bin/python scripts/run_updater.py
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=/opt/alphalens/app/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/alphalens-dashboard.service`:

```ini
[Unit]
Description=AlphaLens Streamlit dashboard
After=network-online.target

[Service]
User=alphalens
WorkingDirectory=/opt/alphalens/app
ExecStart=/opt/alphalens/app/.venv/bin/streamlit run main.py \
    --server.headless=true --server.address=127.0.0.1 --server.port=8501
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=/opt/alphalens/app/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Note `--server.address=127.0.0.1` — same hardening as `.streamlit/config.toml`
on Windows. Manual runs become e.g.
`sudo -u alphalens /opt/alphalens/app/.venv/bin/python scripts/run_updater.py --full`;
the `InterProcessJobLock` serialises them against the daemon exactly as on
Windows.

### A.6 Access & security — the part that matters

The dashboard has no authentication. Anyone reaching :8501 sees holdings and
can trigger EE sync / manage stocks / run backups. Pick one access method:

| Option | Effort | Exposure | Best for |
|---|---|---|---|
| **SSH tunnel** `ssh -L 8501:localhost:8501 user@vm` | zero | none — 8501 never leaves localhost | occasional access |
| **Tailscale** on VM + devices; browse `http://vm-tailnet-ip:8501` | ~20 min | tailnet only; bind Streamlit `0.0.0.0` *with* the security list still blocking 8501 publicly | regular access incl. phone |
| Cloudflare Tunnel + Access (email OTP) | medium | public URL behind Cloudflare auth | sharing with others |
| Public 8501 | — | **never without adding `st.login`** | — |

Recommended: SSH key-only access (OCI default — verify
`PasswordAuthentication no`), UFW allowing only 22, Tailscale for the
dashboard. Optional end-state: private-subnet VM reachable via OCI Bastion
(5 bastions always free) + Tailscale — near-zero attack surface.

**Secrets risk assessment** — `.env` on the VM holds:

- **EasyEquities password — highest risk.** Full brokerage login on an
  internet-facing host. Mitigations: `chmod 600`, dedicated service user,
  SSH keys only, optionally OCI Vault (always free) fetched at service start.
- **Luno key** — already read-only.
- Moneyweb/GoldAPI/etc. — low impact.
- Do **not** commit `.env`; it transits only via scp.

**Other hardening:** `unattended-upgrades` for security patches; fail2ban on
22 (optional with key-only auth); confirm loguru rotates `data/logs/*.log`
or add logrotate. Keep the Windows `AlphaLensUpdater`/`AlphaLensDashboard`
tasks **disabled after migration** — instant rollback path.

**Oracle-specific risks:**

- *Idle reclamation:* Oracle may reclaim Always Free instances idle ~7 days
  (< ~5% CPU/net/disk). The 24/7 daemon + dashboard should stay above the
  floor, but monitor — a reclaimed VM loses its boot volume. Off-box backups
  are not optional.
- *IP reputation:* EE/Yahoo calls from datacenter IPs can hit stricter rate
  limits or fraud checks. EE login from a foreign DC may trigger
  verification — test the sync early; prefer the Johannesburg region if
  available so traffic looks local. If EE blocks DC IPs, keep EE sync on the
  home machine and let the cloud updater handle only market data (the sync
  is a separate callable path).

### A.7 Database choice

Stay on **SQLite**. Single-user, backup machinery already built
(`PRAGMA integrity_check`-verified backups), and Postgres-on-same-VM adds
attack surface and memory for zero benefit. Don't open 5432 anywhere.

### A.8 Off-box backups (required — boot volume is single point of failure)

- Object Storage bucket (20 GB Standard tier always free).
- rclone on the VM, `oci` backend (or S3-compat via customer secret key).
- Cron as `alphalens`, weekly:

```cron
30 19 * * * /usr/bin/rclone sync /opt/alphalens/app/data/backups oci:alphalens-backups --log-file /opt/alphalens/app/data/logs/rclone.log
```

Existing local backups (`_backup_if_due` in the daemon loop) keep working
unchanged; rclone just mirrors them off the boot volume.

### A.9 Migration runbook

1. Create OCI account, pick home region, set $1 budget alert, MFA.
2. Provision A1 VM (retry ADs on capacity errors); security list = 22 only.
3. OS setup + clone + venv (A.4) — `pip install` verifies aarch64 wheels.
4. scp `.env`, `data/stock_analyzer.db`, `data/backups/` to VM; `chmod 600 .env`.
5. Enable + start `alphalens-updater.service`; check `data/logs/updater.log`.
6. Enable + start `alphalens-dashboard.service`; verify via SSH tunnel.
7. Manual `--price-news` and `--backup`; run `scripts/check_stored_prices.py`.
8. rclone → Object Storage cron; verify dumps appear in the bucket.
9. Install Tailscale (optional); test from phone.
10. **Then** disable the Windows tasks (keep installed = rollback).
11. Update `AGENTS.md` — new home, systemd commands, no more Task Scheduler.

### A.10 Cost

$0/month. All Always Free: A1 compute (≤2 OCPU/12 GB), ≤200 GB block,
10 TB egress, Object Storage 20 GB, Bastion. Billable only if exceeding
200 GB block, using paid images, or upgrading (still $0 for these shapes).

---

## Option B — Streamlit Community Cloud + Neon + GitHub Actions

### B.1 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub (PRIVATE repo — required)                           │
│  ├─ Actions: .github/workflows/updater.yml   (cron jobs)    │
│  │     ubuntu-latest → run_updater.py --price-news/--full/  │
│  │     --ipo/--backup + rclone → Cloudflare R2              │
│  └─ source of truth for deploys                             │
└──────────────────────┬──────────────────────────────────────┘
                       │ writes/reads
              ┌────────▼─────────┐          ▲
              │  Neon Postgres   │          │ reads + user writes
              │  free plan: 1 GB │          │
              │  scale-to-zero   │          │
              └──────────────────┘   ┌──────┴───────────────┐
                                     │ Streamlit Community  │
                                     │ Cloud (1 private app)│
                                     │ main.py, OAuth-gated │
                                     └──────────────────────┘
                                     You ──GitHub sign-in──▶
```

The Task Scheduler daemon is replaced by Actions cron; the local
SQLite/Postgres by Neon; the local dashboard by Community Cloud. Nothing
runs on the PC except optionally a fallback.

### B.2 Job mapping (daemon → Actions)

| Windows today | GitHub Actions | Cron (UTC; SAST = UTC+2) |
|---|---|---|
| `--price-news` 09:00/13:00/17:00 | `updater` workflow | `0 7,11,15 * * *` |
| `--full` 18:00 | `updater` workflow | `0 16 * * *` |
| `--ipo` daily 08:00 | `updater` workflow | `0 6 * * *` |
| daemon loop → `_backup_if_due` | `backup` workflow → dump → R2 | `0 19 * * 6` (Sat 21:00 SAST) |
| daemon loop → EE sync (~daily) | — not needed — | fires inside every price run via `_snapshot_portfolio` |
| `--logon`/unlock triggers | — obsolete — | nothing sleeps except Neon, which wakes on connect |

### B.3 Neon setup (~10 min)

1. New project, region **AWS eu-central-1** (Frankfurt).
2. Two connection strings: pooled (`…-pooler.…`) for Actions, direct for
   Streamlit Cloud. Append `?sslmode=require` to both.
3. SQL console, once (belt-and-braces — `base.py` already pins `-c
   timezone=UTC` per connection):

   ```sql
   ALTER DATABASE neondb SET timezone = 'UTC';
   ```

4. `init_database()` against it once from the PC.
5. Migrate data:
   - **From local Postgres:** `pg_dump -Fc` → `pg_restore` into Neon.
   - **From SQLite:** `python scripts\migrate_sqlite_to_postgres.py` with
     `DATABASE_URL` pointed at Neon (idempotent, preserves PKs).
6. Free-plan caps: **1 GB storage, 100 CU-hours/mo, 5 GB egress/mo,
   scale-to-zero after 5 min** (cannot be disabled on free). The 8.3 MB DB
   and ~10 min/day of active queries are orders of magnitude under;
   `pool_pre_ping=True` already handles suspended-compute stale connections.

### B.4 GitHub Actions

`.github/workflows/updater.yml`:

```yaml
name: AlphaLens updater
on:
  schedule:
    - cron: '0 7,11,15 * * *'   # --price-news 09/13/17 SAST
    - cron: '0 16 * * *'        # --full 18:00 SAST
    - cron: '0 6 * * *'         # --ipo 08:00 SAST
  workflow_dispatch:
    inputs:
      mode:
        type: choice
        options: [--price-news, --full, --ipo, --backup, --logon]

concurrency:
  group: alphalens-updater        # serialized across schedules
  cancel-in-progress: false

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
          cache: pip
      - run: pip install -r requirements.txt
      - name: Pick mode
        id: mode
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            echo "arg=${{ inputs.mode }}" >> "$GITHUB_OUTPUT"; exit 0
          fi
          case "${{ github.event.schedule }}" in
            "0 16 * * *") echo "arg=--full"       >> "$GITHUB_OUTPUT" ;;
            "0 6 * * *")  echo "arg=--ipo"        >> "$GITHUB_OUTPUT" ;;
            *)            echo "arg=--price-news" >> "$GITHUB_OUTPUT" ;;
          esac
      - name: Run updater
        env:                       # all from repo → Settings → Secrets
          DATABASE_URL: ${{ secrets.NEON_POOLED_URL }}
          TWELVE_DATA_API_KEY: ${{ secrets.TWELVE_DATA_API_KEY }}
          TWELVE_DATA_ENABLED: "false"
          EASYEQUITIES_USERNAME: ${{ secrets.EASYEQUITIES_USERNAME }}
          EASYEQUITIES_PASSWORD: ${{ secrets.EASYEQUITIES_PASSWORD }}
          LUNO_API_KEY_ID: ${{ secrets.LUNO_API_KEY_ID }}
          LUNO_API_SECRET: ${{ secrets.LUNO_API_SECRET }}
          MONEYWEB_USERNAME: ${{ secrets.MONEYWEB_USERNAME }}
          MONEYWEB_PASSWORD: ${{ secrets.MONEYWEB_PASSWORD }}
          GOLD_API_KEY: ${{ secrets.GOLD_API_KEY }}
          METALS_DEV_API_KEY: ${{ secrets.METALS_DEV_API_KEY }}
          ALPHA_VANTAGE_API_KEY: ${{ secrets.ALPHA_VANTAGE_API_KEY }}
          NEWSDATA_API_KEY: ${{ secrets.NEWSDATA_API_KEY }}
          SCHEDULER_TIMEZONE: Africa/Johannesburg
          NOTIFICATION_ENABLED: "true"
          NOTIFICATION_NTFY_TOPIC: ${{ secrets.NOTIFICATION_NTFY_TOPIC }}
        run: python scripts/run_updater.py ${{ steps.mode.outputs.arg }}
```

Separate `backup.yml` (weekly): same setup plus
`sudo apt-get install -y postgresql-client` (pg_dump + pg_restore for
`backup.py`'s verification), then `--backup`, then
`rclone copy data/backups r2:alphalens-backups` (R2: 10 GB free, zero egress
fees; `RCLONE_CONFIG_*` env secrets). Add a monthly trivial-commit step to
keep the repo "active" against scheduled-workflow auto-disable.

**Cron gotchas:** scheduled runs can be delayed 5–30 min under load (the
17:00 SAST fetch may land 17:15+ — fine, EOD prices persist) and silently
skipped at peak times; scheduled workflows are auto-disabled after ~60 days
of repo inactivity (documented for public repos, reported on private ones
too — hence the keepalive commit). `workflow_dispatch` on everything allows
manual backfill.

**Minutes math (private repo):** Free plan = $12/mo compute credit ≈ 1,500
ubuntu minutes. Estimate: ~90 price-news × 3 min + ~30 full × 12 min + IPO
≈ 700–900 min/mo — fits, but `--full` regressions could push it over;
watch the billing page.

### B.5 Streamlit Community Cloud

1. Repo must be **private** → the deploy is the **one free private app**,
   gated by GitHub OAuth. A public repo would produce a *public* app
   exposing holdings + sync buttons — non-negotiable.
2. Deploy: main file `main.py`, Python 3.13, `.env` contents into the
   secrets TOML (same keys — see B.6 shim).
3. ~1 GB RAM per app — fine for the dashboard; leave heavy rebuilds to
   Actions.
4. Apps sleep after inactivity → cold start ~10–30 s + Neon wake ~1 s on
   first query.
5. `.streamlit/config.toml` pins `server.address = "localhost"` — Community
   Cloud's own `--server.address` CLI flag outranks config.toml, so it
   should be ignored; verify the app serves, and remove the line from
   tracked config if it breaks (localhost enforcement on Windows lives in
   `run_dashboard.py`/env regardless).

### B.6 Required code changes

| # | Change | Why |
|---|---|---|
| 1 | **`st.secrets` → env bridge.** Top of `main.py`, before any `app.*` import: `for k, v in st.secrets.items(): os.environ.setdefault(k, str(v))` (guarded — `st.secrets` raises outside Streamlit runtime). pydantic-settings reads env vars + `.env`, but Cloud secrets are only exposed via `st.secrets`. | Without it the Cloud app boots with placeholder settings and fails. |
| 2 | **Cross-host job lock.** `InterProcessJobLock` is file-based — its own docstring admits it can't stop a daemon on another host sharing one Postgres. "Refresh Prices" on Cloud + an Actions run can overlap → double API spend + write races. Add `pg_try_advisory_lock(<const>)` on a dedicated connection when the dialect is postgresql; keep the file lock for SQLite. | Real race this architecture exposes. |
| 3 | **`data/` is ephemeral everywhere.** Logs and `.updater.lock` vanish on every Cloud reboot/redeploy — acceptable (durability lives in `updater_runs`/`notifications`). Backups must **only** run in Actions; if the Settings page exposes a backup button it should be labeled/hidden when `data/` is ephemeral. | Prevent false confidence in vanishing files. |
| 4 | Nothing else — `pool_pre_ping`, UTC session pinning, `day_of()` dialect handling, `_migrate_schema` are already Postgres-ready. | — |

### B.7 Secrets inventory (two stores, same values)

`st.secrets` (Cloud dashboard) ⊇ GitHub secrets (Actions). Both need:
`DATABASE_URL` (different values — direct vs pooler), `TWELVE_DATA_*`,
`EASYEQUITIES_*`, `LUNO_*`, `MONEYWEB_*`, `GOLD_API_KEY`, `METALS_DEV_*`,
`ALPHA_VANTAGE_*`, `NEWSDATA_API_KEY`, `NOTIFICATION_*`. Duplication is a
drift risk — rotate in both places.

### B.8 Security assessment

- **Dashboard exposure:** private app behind GitHub OAuth is roughly
  equivalent to the localhost binding — arguably better (authenticated
  identity vs network position). But a repo flipped public → instantly
  public app. Keep it private forever.
- **Secrets sprawl:** EE password + Luno + Moneyweb creds live in two
  third-party stores (Streamlit/Snowflake, GitHub). Reasonable custodians,
  but the EE password's blast radius is unchanged — full brokerage access.
  No way to keep it local *and* have cloud sync.
- **Datacenter-IP blocking — the real wildcard:** Moneyweb scraping (SENS,
  authenticated collectors) and EasyEquities' unofficial API from Streamlit
  Cloud (GCP) and Actions (Azure) IPs may hit anti-bot walls a home IP
  doesn't. **Test before committing:** a `workflow_dispatch` `--ipo` run and
  an EE sync reveal it in minutes. If EE blocks DC IPs, run
  `scripts/sync_easyequities.py` from home nightly and let the cloud do
  market data only — the sync is fully decoupled.
- **Neon is internet-facing** with only password + TLS (no IP allow-list on
  free). Long random password; `DATABASE_URL` leak = total data access.
- **GitHub account = new root of trust:** repo access grants secrets
  (Actions) + the app. MFA on GitHub is mandatory.

### B.9 Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| EE/Moneyweb blocks datacenter IPs | medium | sync/scrapers fail silently | test day 1; run EE sync locally; RSS still works |
| Actions cron drift/skip | common | late/stale fetches | app already surfaces staleness ("last market data" caption); dispatch manually |
| Workflow auto-disabled (60-day) | possible | **all updates stop, silently** | monthly keepalive commit + existing `updater_runs` staleness checks in the UI |
| Compute credit exhausted | low | runs blocked until month-end | watch Actions billing; trim `--full` frequency |
| Streamlit Cloud RAM/OOM on heavy views | low-medium | app restart | avoid full-history rebuilds from UI |
| Neon suspend cold-start latency | certain | ~1 s extra on first query | already handled by `pool_pre_ping` |
| Secret drift between stores | certain over time | one side silently misconfigured | document both lists in `AGENTS.md` |

### B.10 Migration runbook

1. Confirm repo is private. Push latest.
2. Neon project → `ALTER DATABASE … SET timezone='UTC'` → `init_database()`
   → migrate data (B.3.5).
3. Point local `.env` `DATABASE_URL` at Neon temporarily; run the dashboard
   locally against it for a day — proves Postgres correctness first.
4. Add workflow files; create GH secrets; `workflow_dispatch` a
   `--price-news` run → check the `updater_runs` row appears.
5. Dispatch `--ipo` and an EE sync — **the datacenter-IP smoke test**.
6. Add the secrets shim to `main.py`; deploy on Community Cloud; verify the
   private app behind GitHub sign-in; fix `server.address` if needed.
7. Enable the crons; verify the 07:00 UTC run lands.
8. Weekly backup workflow → R2; verify dump in bucket.
9. **Then** disable the Windows tasks (keep installed = rollback).
10. Update `AGENTS.md`: new topology, the advisory-lock change, secrets in
    two stores, `data/` is ephemeral.

### B.11 Cost

$0/month: Community Cloud free tier · Neon free plan (1 GB / 100 CU-hr /
5 GB egress) · Actions within the $12 credit · R2 free 10 GB. The durable
constraint is the Actions minutes budget — everything else has years of
headroom.

---

## Comparison

Both options are genuinely $0/month and both preserve the app's existing
behaviour almost unchanged — the differences are in *where the risk sits*:

- **Option A** concentrates everything on one VM you fully control. One
  secrets store, one network boundary, systemd instead of Task Scheduler —
  conceptually a lift-and-shift of the current Windows setup. The unique
  risks are operational: A1 capacity lottery, Oracle's idle-reclaim policy,
  and you being the sysadmin for a machine holding your brokerage password.
- **Option B** distributes the app across three managed services with
  always-on free tiers. No sysadmin work, but it requires ~2 real code
  changes (secrets shim, advisory lock), splits secrets across two stores,
  replaces the durable daemon with delay-prone cron, and puts your most
  sensitive scrapers on datacenter IPs that may be blocked. Its failure mode
  is *silent*: a disabled workflow or blocked sync looks like "no data",
  not an error.

The tiebreaker is the EE question: on Oracle the EE password sits on a VM
only you can reach; on Option B it sits in GitHub + Streamlit secret stores
*and* the sync runs from datacenter IPs that may not work at all. If EE
sync is important, Option A is the safer bet — or keep the split but run
`sync_easyequities.py` from home.

| | Option A — Oracle Always Free VM | Option B — Streamlit Cloud + Neon + Actions |
|---|---|---|
| **Cost** | $0 forever (Always Free tier) | $0 within quotas (Actions minutes is the binding one) |
| **Code changes** | None | ~2 real changes: `st.secrets` shim, Postgres advisory lock (+ possibly `server.address` fix) |
| **Infra to maintain** | One VM: patching, ufw, systemd, monitoring | None — all managed services |
| **Dashboard access** | SSH tunnel / Tailscale — private by construction | GitHub-OAuth private app — private by policy (breaks if repo goes public) |
| **Database** | SQLite on local disk, familiar | Neon Postgres, scale-to-zero, 1 GB cap |
| **Scheduler** | systemd daemon — same reliability as today | Actions cron — 5–30 min drift, silent skips, 60-day auto-disable risk |
| **Secrets** | One `.env`, `chmod 600`, one host | Two stores (Streamlit + GitHub), drift risk |
| **EE sync from datacenter IP** | May still be flagged, but VM IP is yours; JNB region keeps it local | Higher risk — shared GCP/Azure IP ranges |
| **Backups** | Existing `backup.py` + rclone to Object Storage | pg_dump in Actions + rclone to R2 — `data/` ephemeral so backups must live off-runner |
| **Failure modes** | VM dies / reclaimed — loud and obvious | Silent: disabled cron, exhausted credit, blocked scraper |
| **Cold start** | None — always on | App sleep (~30 s) + Neon wake (~1 s) |
| **Pros** | • Whole app unchanged, one host<br>• No code changes<br>• Strongest access control (no public surface at all)<br>• 12 GB RAM — no memory ceiling<br>• Survives every free-tier change: it's just a VM | • Zero sysadmin work<br>• Managed Postgres with point-in-time snapshots<br>• OAuth-gated dashboard = authenticated access<br>• Each tier independently replaceable<br>• Dashboard usable from any browser with no VPN |
| **Cons** | • A1 capacity often unavailable; signup friction<br>• Oracle can reclaim idle instances — lose the boot volume<br>• You own OS security/patching forever<br>• Single point of failure<br>• Remote access needs Tailscale/SSH — not a shareable URL | • Requires code changes (secrets shim, advisory lock)<br>• Secrets duplicated in two third-party stores<br>• Cron drift/skips + silent 60-day disable<br>• Datacenter-IP blocking of EE/Moneyweb scrapers<br>• 1 GB DB cap, 1 GB app RAM, $12 Actions credit are hard ceilings<br>• Ephemeral `data/` — nothing local survives a redeploy |
