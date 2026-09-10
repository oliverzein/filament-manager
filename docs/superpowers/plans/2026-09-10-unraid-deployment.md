# Unraid Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package filament-manager v2.0.0 as Docker image, configure Unraid DockerMan template, and deploy container to Unraid host (`192.168.0.31`) with persistent appdata and printer connectivity.

**Architecture:** Docker container running FastAPI/uvicorn on port 8000 mapped to host port 8404. Appdata volume bind-mounted to `/mnt/user/appdata/filament-manager` holding SQLite DB (`filaments.db`) and configuration (`settings.json`). DockerMan template installed to `/boot/config/plugins/dockerMan/templates-user/my-filament-manager.xml`.

**Tech Stack:** Docker, Unraid DockerMan (XML v2), Bash, Python 3.12, SQLite, FastAPI.

**Spec / Live Constraints:**
- Unraid host: `unraid` (`192.168.0.31`), access via SSH alias `ssh root@unraid`.
- Free host port: `8404` (verified no collision on Unraid).
- Printer IP: `192.168.0.101` (verified reachable from Unraid with 2ms latency).
- Remote directory: `/mnt/user/appdata/filament-manager`.
- Unraid template standard: root tag `<Container version="2">`, filename `my-filament-manager.xml` in `/boot/config/plugins/dockerMan/templates-user/`.

---

### Task 1: Commit Clean Working Tree

**Files:**
- Modify: 18 modified files currently in working tree (spool table Excel filter dialogs, sort arrows, status class cleanup, doc updates).

- [ ] **Step 1: Verify test suite passes**
Run: `scripts/test.sh`
Expected: 354 passed.

- [ ] **Step 2: Commit pending changes**
Run:
```bash
git add README.md docs/ fm/ tests/
git commit -m "feat: spool inventory filtering, sorting improvements, and doc updates"
```

- [ ] **Step 3: Verify clean working tree**
Run: `git status --porcelain`
Expected: empty output.

---

### Task 2: Fix Unraid DockerMan Template & Packaging Scripts

**Files:**
- Modify: `deploy/filament-manager.xml`
- Modify: `scripts/deploy-unraid.sh`
- Modify: `scripts/release.sh`
- Create: `deploy/filament-manager.env.example`
- Modify: `pyproject.toml` (package-data inclusion)

- [ ] **Step 1: Ensure pyproject.toml includes all package data**
Add `package-data` so all html, css, json, and system templates are guaranteed inside the built package:
```toml
[tool.setuptools.package-data]
"*" = ["*.html", "*.css", "*.js", "*.json", "*.svg", "*.png"]
```

- [ ] **Step 2: Rewrite `deploy/filament-manager.xml` to Unraid `<Container version="2">` format**
Replace `<Containers><Container>...</Container></Containers>` with standard Unraid XML:
```xml
<?xml version="1.0"?>
<Container version="2">
  <Name>filament-manager</Name>
  <Repository>filament-manager:2.0.0</Repository>
  <Registry/>
  <Network>bridge</Network>
  <MyIP/>
  <Shell>sh</Shell>
  <Privileged>false</Privileged>
  <Support/>
  <Project>https://github.com/oliverzein/filament-manager</Project>
  <Overview>Web app to manage 3D-printing filament spool inventory and sync to Creality K2 printer DB from Orca Cloud presets.</Overview>
  <Category>Productivity: Tools:Utilities</Category>
  <WebUI>http://[IP]:[PORT:8404]/</WebUI>
  <TemplateURL/>
  <Icon/>
  <ExtraParams/>
  <PostArgs/>
  <CPUset/>
  <DateInstalled>1789027200</DateInstalled>
  <Config Name="WebUI Port" Target="8000" Default="8404" Mode="tcp" Description="Container port 8000 mapped to host port 8404." Type="Port" Display="always" Required="true" Mask="false">8404</Config>
  <Config Name="App Data" Target="/data" Default="/mnt/user/appdata/filament-manager" Mode="rw" Description="Bind-mounted app data directory holding SQLite database and settings.json." Type="Path" Display="always" Required="true" Mask="false">/mnt/user/appdata/filament-manager</Config>
  <Config Name="FM_DATABASE_URL" Target="FM_DATABASE_URL" Default="sqlite:////data/filaments.db" Mode="" Description="Database URL inside container." Type="Variable" Display="always" Required="true" Mask="false">sqlite:////data/filaments.db</Config>
  <Config Name="FM_PRINTER_IP" Target="FM_PRINTER_IP" Default="192.168.0.101" Mode="" Description="Creality K2 printer IP." Type="Variable" Display="always" Required="true" Mask="false">192.168.0.101</Config>
  <Config Name="FM_SSH_USER" Target="FM_SSH_USER" Default="root" Mode="" Description="SSH user for printer." Type="Variable" Display="always" Required="true" Mask="false">root</Config>
  <Config Name="FM_SSH_PASSWORD" Target="FM_SSH_PASSWORD" Default="" Mode="" Description="SSH password for printer." Type="Variable" Display="always" Required="true" Mask="true">creality_2024</Config>
  <Config Name="FM_DB_REMOTE_PATH" Target="FM_DB_REMOTE_PATH" Default="/mnt/UDISK/creality/userdata/box/material_database.json" Mode="" Description="Remote printer DB path." Type="Variable" Display="advanced" Required="true" Mask="false">/mnt/UDISK/creality/userdata/box/material_database.json</Config>
  <Config Name="FM_WS_PORT" Target="FM_WS_PORT" Default="9999" Mode="" Description="Printer websocket port." Type="Variable" Display="advanced" Required="false" Mask="false">9999</Config>
  <Config Name="FM_VERSION_OVERRIDE" Target="FM_VERSION_OVERRIDE" Default="9876543210" Mode="" Description="Printer DB version override." Type="Variable" Display="advanced" Required="false" Mask="false">9876543210</Config>
  <Config Name="FM_ID_RANGE_START" Target="FM_ID_RANGE_START" Default="99001" Mode="" Description="Start of custom printer ID range." Type="Variable" Display="advanced" Required="false" Mask="false">99001</Config>
</Container>
```

- [ ] **Step 3: Update `scripts/release.sh`**
Tag both `filament-manager:${version}` and `filament-manager:latest` so both tags are stored in the tarball:
```bash
docker tag "${image}" "filament-manager:latest"
docker save "${image}" "filament-manager:latest" | gzip > "${tarball}"
```

- [ ] **Step 4: Update `scripts/deploy-unraid.sh`**
- Target template file on Unraid: `${templates_dir}/my-filament-manager.xml` (Unraid user template convention).
- Create remote appdata directory `/mnt/user/appdata/filament-manager` on Unraid BEFORE scp.
- If `${env_file}` does not exist, auto-create a minimal `${env_file}` (`FM_DATABASE_URL=sqlite:////data/filaments.db`) instead of hard exiting.
- Ensure proper permissions on `/mnt/user/appdata/filament-manager`.

- [ ] **Step 5: Create `deploy/filament-manager.env.example`**
```bash
FM_DATABASE_URL=sqlite:////data/filaments.db
FM_PRINTER_IP=192.168.0.101
FM_SSH_USER=root
FM_SSH_PASSWORD=creality_2024
FM_DB_REMOTE_PATH=/mnt/UDISK/creality/userdata/box/material_database.json
FM_WS_PORT=9999
FM_VERSION_OVERRIDE=9876543210
FM_ID_RANGE_START=99001
```

- [ ] **Step 6: Commit changes**
```bash
git add deploy/ scripts/ pyproject.toml
git commit -m "fix(deploy): update Unraid template to v2 schema, improve deploy and release scripts"
```

---

### Task 3: Build & Release Tarball

- [ ] **Step 1: Run release script**
Run: `scripts/release.sh 2.0.0`
Expected: creates `dist/filament-manager-2.0.0.tar.gz` and prints sha256 checksum.

- [ ] **Step 2: Verify tarball contents**
Run: `tar -ztvf dist/filament-manager-2.0.0.tar.gz | head -n 10`
Expected: valid gzip docker archive.

---

### Task 4: Seed Appdata & Deploy Container to Unraid

- [ ] **Step 1: Create remote appdata directory on Unraid**
Run: `ssh root@unraid "mkdir -p /mnt/user/appdata/filament-manager"`

- [ ] **Step 2: Seed settings & database from local environment**
Copy `.local/settings.json` (contains printer IP, SSH pass, Orca refresh token, GitHub token) and `.local/filaments.db` (36 spools, cached profiles) to `/mnt/user/appdata/filament-manager/`:
```bash
scp -O .local/settings.json root@unraid:/mnt/user/appdata/filament-manager/settings.json
scp -O .local/filaments.db root@unraid:/mnt/user/appdata/filament-manager/filaments.db
ssh root@unraid "chmod 664 /mnt/user/appdata/filament-manager/settings.json /mnt/user/appdata/filament-manager/filaments.db"
```

- [ ] **Step 3: Create deploy/filament-manager.env**
Copy `deploy/filament-manager.env.example` to `deploy/filament-manager.env`.

- [ ] **Step 4: Execute deployment script**
Run: `scripts/deploy-unraid.sh 2.0.0`
Expected:
- Copy tarball to Unraid
- Copy template to `/boot/config/plugins/dockerMan/templates-user/my-filament-manager.xml`
- `docker load` image
- `docker run` container
- Healthcheck polling returns: `filament-manager 2.0.0 is up at http://192.168.0.31:8404`

---

### Task 5: Live Verification & Knowledge Retention

- [ ] **Step 1: Verify endpoints against live Unraid container**
```bash
curl -sf http://192.168.0.31:8404/api/version
curl -sf http://192.168.0.31:8404/ | grep -q "Filament Manager"
curl -sf http://192.168.0.31:8404/spools | grep -q "Spool Inventory"
curl -sf http://192.168.0.31:8404/sync | grep -q "Sync Hub"
curl -sf http://192.168.0.31:8404/settings | grep -q "Settings"
```

- [ ] **Step 2: Verify Unraid template registration**
Run: `ssh root@unraid "ls -la /boot/config/plugins/dockerMan/templates-user/my-filament-manager.xml"`
Expected: file exists and readable.

- [ ] **Step 3: Document deployment in BrainVault**
Create/update note in `BrainVault/projects/filament-manager/unraid-deployment.md` with:
- Container name, IP, port (8404)
- Bind-mount paths (`/mnt/user/appdata/filament-manager`)
- Template location (`my-filament-manager.xml`)
- Verification results
