<div align="center">

<img src="docs/banner.svg" alt="flop-pc-miner" width="100%"/>

**Run a FLOP devnet miner on your own gaming PC: Windows (WSL2) with double-click start and stop, or native Linux in a terminal, official images only, and a live dashboard.**

[![Live miner](https://img.shields.io/badge/LIVE%20MINER-explorer-3ddc97?style=for-the-badge)](https://explorer.devnet.flop.network/account/5ECEvG31hJ6jprGcAYRHMchj98k4c4A3RAGvNqGynx4ffKvW)
[![First settlement](https://img.shields.io/badge/FIRST%20SETTLEMENT-tx-21c7f2?style=for-the-badge)](https://explorer.devnet.flop.network/transactions/0x65e921ec5cb965e88b712a2d552d5194a550e21a785b3229cceecfea188ba3be)
[![Miner board](https://img.shields.io/badge/DEVNET%20BOARD-live-e6a93a?style=for-the-badge)](https://flop-devnet-board.vercel.app)
[![License](https://img.shields.io/badge/LICENSE-MIT-white?style=for-the-badge)](LICENSE)

![Windows 11](https://img.shields.io/badge/Windows-10%2F11-0078D4?logo=windows)
![WSL2](https://img.shields.io/badge/WSL2-Ubuntu%2024.04-E95420?logo=ubuntu&logoColor=white)
![NVIDIA](https://img.shields.io/badge/NVIDIA-tested%20RTX%205080-76B900?logo=nvidia&logoColor=white)
![Docker](https://img.shields.io/badge/Docker%20Engine-official%20apt-2496ED?logo=docker&logoColor=white)
![Python](https://img.shields.io/badge/dashboard-Python%20stdlib-3776AB?logo=python&logoColor=white)

</div>

> **Fork credit:** this is a fork of [Makabeez/flop-pc-miner](https://github.com/Makabeez/flop-pc-miner) (MIT) — all credit for the original Windows/WSL2 setup, the security model and the dashboard goes to [@Makabeez](https://github.com/Makabeez). This fork adds native Linux support (`linux/install.sh`, WSL-free `flop-miner.sh` v2.0.0); everything Windows works exactly as upstream wrote it.

> **It works.** This setup runs a home RTX 5080 miner (`5ECEvG31…ffKvW`, Soft tier, 28 GFLOP/s cap). After Flop Labs' fix on 8 Nov 2026, **11 of the next 13 gateway sessions settled on chain within 5½ hours, including every one of the last 10 (6.43 test FLOP)**. The first settlement was [`0x65e921ec…a3be`](https://explorer.devnet.flop.network/transactions/0x65e921ec5cb965e88b712a2d552d5194a550e21a785b3229cceecfea188ba3be): 134 GFLOP served, 0.8138 FLOP to the miner. Every error that came up on the way, and its fix, is in [Troubleshooting](#troubleshooting).

> **Devnet only.** Devnet FLOP has no value. Flop Labs says devnet mining earns no rewards and no airdrop weight, and the chain may be reset. Do it to learn, to test, and to be an early working miner. Not affiliated with Flop Labs.

## Contents

1. [What this is](#what-this-is)
2. [Is my PC enough?](#is-my-pc-enough)
3. [What it costs to run](#what-it-costs-to-run)
4. [Security: what runs, what is exposed](#security-what-runs-what-is-exposed)
5. [How it fits together](#how-it-fits-together)
6. [Install, step by step](#install-step-by-step)
7. [Install on native Linux, step by step](#install-on-native-linux-step-by-step)
8. [Daily use](#daily-use)
9. [The dashboard and widget](#the-dashboard-and-widget)
10. [Is it working? Am I getting paid?](#is-it-working-am-i-getting-paid)
11. [Settings](#settings)
12. [Troubleshooting](#troubleshooting)
13. [Updating and uninstalling](#updating-and-uninstalling)
14. [FAQ](#faq)
15. [Credits](#credits)

---

## What this is

FLOP is a compute network. Miners run an AI model on their GPU and serve inference to buyers through Flop's gateway. Each job runs through a **compute channel**: the buyer locks payment in escrow, your miner answers, and the channel **settles on chain** with a proof of the work. On the devnet, the buyer is Flop's hosted gateway and the payment is test FLOP.

Flop's own miner software ships as two Docker images:

| Image | What it does |
|---|---|
| `miner-vllm-toploc` (backend) | Runs the model (Qwen2.5-0.5B-Instruct) on your GPU with vLLM and produces the TOPLOC activation proofs |
| `miner-worker` | Holds your miner key, talks to the chain, answers network test jobs, serves gateway sessions, settles channels |

This repo wraps them for a **Windows PC with an NVIDIA GPU**:

- `flop-miner.sh`: one script that installs, verifies, registers, starts and stops everything inside a dedicated WSL2 distro
- `flop-dash.py`: a live local dashboard and a compact widget, with sound alerts
- `windows/`: an installer and five Desktop shortcuts (Start, Stop, Status, Dashboard, Widget)

You never need a terminal open while mining.

## Is my PC enough?

| | Needed | Tested |
|---|---|---|
| OS | Windows 10 22H2 or Windows 11, with WSL2 | Windows 11 |
| GPU | NVIDIA, recent driver. The model is small (0.5B); the backend reserves 60% of VRAM by default | RTX 5080 16 GB |
| VRAM | 12 GB+ comfortable; 8 GB may work with `GPU_UTIL=0.5` (untested) | 16 GB (about 13 GB used while mining) |
| Disk | **40 GB free** on C: (the backend image is about 23 GB) | |
| RAM | 16 GB+ | 64 GB |
| Network | Stable connection. **No port forwarding**: the miner reaches out through a Cloudflare tunnel | Home fibre |

The model and decode policy come from Flop's official [`releases.json`](https://flop.network/releases.json); the script picks them up automatically.

## What it costs to run

The GPU barely works between jobs. Measured on the RTX 5080: **8–15 W idle**, with short bursts of **30–60 W** while serving a session. Most of the cost is keeping the PC on.

```
monthly cost ≈ PC idle watts × 720 h ÷ 1000 × your price per kWh
example: 90 W × 720 h = 65 kWh × CHF 0.27 (Geneva) ≈ CHF 18 / month
```

For comparison, renting a cloud GPU to do the same costs over CHF 100 per month. If your PC is already on for other reasons, the extra cost is a few francs.

## Security: what runs, what is exposed

Everything here was written to be checked, not trusted:

- **Official images only.** The script reads Flop's [`releases.json`](https://flop.network/releases.json) and pulls `ghcr.io/flop-labs/flop-core/*` **by sha256 digest**. It refuses any image reference that is not from that org and pinned by digest. The org is listed on [flop.finance/official](https://flop.finance/official).
- **Official system packages.** Docker Engine (download.docker.com), the NVIDIA Container Toolkit (nvidia.github.io) and cloudflared (pkg.cloudflare.com) all come from their signed apt repositories. Docker Desktop is not used.
- **Its own distro.** Everything lives in a separate WSL distro called `flop-miner`, not your everyday Ubuntu. After `harden`, that distro cannot see your Windows drives or launch Windows programs. The GPU still works.
- **Nothing listens on your network.** The backend has no published port. The worker listens on `127.0.0.1` only. The only way in from the internet is a Cloudflare quick tunnel to the worker's session port, which answers `401` without a 64-character session token.
- **A new, devnet-only key.** `setup` generates a fresh miner key inside the distro and never prints it. Your real wallets are never involved, and nothing ever asks for a seed phrase. The seed files are root-owned, mode `600`, in `/opt/flop-miner`.
- **The stake comes from Flop's sponsor.** The sponsor request sends only your miner address, a timestamp and a signature, and the script refuses to sign if the sponsor reports a different chain.
- **The dashboard is local and read-only.** It binds to `127.0.0.1:8650` and never reads or serves your seeds or tokens.

Read [`flop-miner.sh`](flop-miner.sh) before running it. It is one file, and every step is commented.

## How it fits together

```
 Windows PC ───────────────────────────────────────────────────────────────────────────┐
 │  Desktop shortcuts ── wsl.exe ──┐          Edge widget ── http://127.0.0.1:8650     │
 │                                 ▼                                │                  │
 │  WSL2 distro "flop-miner" (Ubuntu 24.04, cut off from C:\ after harden)             │
 │   ┌──────────────────────────────────────────────────────────────┴──────────────┐   │
 │   │ flop-dash.py :8650 (local only)  reads nvidia-smi, docker, chain, indexer   │   │
 │   │                                                                             │   │
 │   │ docker compose "flop-miner"                                                 │   │
 │   │   backend  miner-vllm-toploc  ── GPU (via the Windows NVIDIA driver)        │   │
 │   │   worker   miner-worker  127.0.0.1:8645  ── miner key (root, 600)           │   │
 │   │                                    ▲                                        │   │
 │   │ cloudflared quick tunnel ──────────┘  https://<random>.trycloudflare.com    │   │
 │   └─────────────────────────────────────────────────────────────────────────────┘   │
 └──────────────────────────────────────────────────────────────────────────────────────┘
        │ outbound only                           ▲ gateway sessions (token required)
        ▼                                         │
  FLOP devnet: rpc.devnet.flop.network · mailbox · faucet sponsor · api.flop.network gateway
```

## Install, step by step

Allow about an hour, most of it downloading. Open a **new PowerShell window** for each block, then copy and paste.

### 0. Windows prerequisites

1. Install the latest **NVIDIA driver** for your GPU (GeForce Experience / NVIDIA App, or nvidia.com). Do **not** install a driver inside Linux; WSL uses the Windows one.
2. Install or update WSL:

```powershell
wsl --install --no-distribution
wsl --update
```

Reboot if it asks.

### 1. Create the dedicated distro

```powershell
wsl --install Ubuntu-24.04 --name flop-miner
```

It asks for a **new Linux username and password**. Pick anything and remember the password: the scripts use `sudo`. When you get a Linux prompt, type `exit`.

> If `--name` is not recognised, run `wsl --update` and try again. Always target this distro with `wsl -d flop-miner`; plain `wsl` opens your default distro instead.

### 2. Get this repo and run the installer

```powershell
cd $env:USERPROFILE\Downloads
Invoke-WebRequest https://github.com/Makabeez/flop-pc-miner/archive/refs/heads/main.zip -OutFile flop-pc-miner.zip
Expand-Archive flop-pc-miner.zip -DestinationPath . -Force
cd flop-pc-miner-main
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
```

The installer copies `flop-miner.sh` and `flop-dash.py` into the distro (it prints the sha1 of each copied file) and puts five `FLOP Miner - …` shortcuts on your Desktop. It downloads nothing and needs no admin rights.

### 3. Install Docker and the NVIDIA toolkit inside the distro

```powershell
wsl -d flop-miner --cd ~ -- ./flop-miner.sh prereqs
wsl --terminate flop-miner
```

It ends by running `nvidia-smi -L` inside a container, which must list your GPU. The `terminate` applies your new `docker` group membership.

### 4. Set up the miner

Pick a public worker name, then run setup:

```powershell
wsl -d flop-miner --cd ~ -- bash -lc "WORKER_NAME=yourname-4090 ./flop-miner.sh setup"
```

What `setup` does, in order:

1. Reads `releases.json`, checks every image reference, and picks the model and decode policy.
2. Writes `/opt/flop-miner/miner.env` (mode 600) with random 64-character API and session tokens.
3. Pulls the images by digest. This takes a while: about 23 GB.
4. Generates your miner key and prints the **miner address**.
5. Asks Flop's **stake sponsor** for a stake, then waits as long as the sponsor asks between tries. The sponsor can be busy or full: at most 40 uncalibrated miners at a time. Pressing Ctrl+C is safe; continue later with `./flop-miner.sh sponsor`.
6. Downloads the model with Flop's CLI, which checks every file against the on-chain record.
7. Installs cloudflared and the `flop-tunnel` service.

### 5. Lock the distro down (recommended)

```powershell
wsl -d flop-miner --cd ~ -- ./flop-miner.sh harden
wsl --terminate flop-miner
wsl -d flop-miner -- bash -lc "ls /mnt/c 2>&1 | head -1; docker run --rm --gpus all nvidia/cuda:13.0.1-base-ubuntu22.04 nvidia-smi -L"
```

The first line must be empty or show an error, because C:\ is no longer visible. The second must list your GPU.

### 6. Start mining

Double-click **FLOP Miner - Start** on your Desktop. It asks for your Linux password once, then:

1. starts the tunnel and records its new `https://….trycloudflare.com` address
2. loads the model onto the GPU
3. starts the worker, which publishes your address to Flop's mailbox
4. opens the dashboard in your browser

In the first few hours the network runs **calibration**: a burst of test jobs that measures your GPU and records a cap (GFLOP/s) and a tier on chain. On the test PC it took about 3 hours from registration. After that, the gateway starts opening sessions with you.

## Install on native Linux, step by step

If you already run Linux on the PC (Ubuntu 24.04-class, or a 24.04-based derivative like Pop!_OS), skip WSL entirely: the same `flop-miner.sh` runs natively, with the same commands, the same security model and the same official images. Allow about an hour, most of it downloading.

### 0. Linux prerequisites

1. Install the latest **NVIDIA driver** for your GPU (`ubuntu-drivers`, or nvidia.com). Do **not** install the driver inside Docker; the host driver is shared with the containers.
2. Check the GPU is visible:

```bash
nvidia-smi -L
```

### 1. Get this repo and run the installer

```bash
cd ~/Downloads
git clone https://github.com/Makabeez/flop-pc-miner.git
cd flop-pc-miner
./linux/install.sh
```

The installer checks the host and the GPU, copies `flop-miner.sh` and `flop-dash.py` into your home directory (it prints the sha1 of each copied file) and downloads nothing. Re-run it any time to update.

### 2. Install Docker and the NVIDIA toolkit

```bash
cd
./flop-miner.sh prereqs
```

It uses `sudo` to add Docker's and NVIDIA's official signed apt repositories, then runs `nvidia-smi -L` inside a container, which must list your GPU.

### 3. Set up the miner

Pick a public worker name, then run setup:

```bash
WORKER_NAME=yourname-5080 ./flop-miner.sh setup
```

What `setup` does, in order:

1. Reads `releases.json`, checks every image reference, and picks the model and decode policy.
2. Writes `/opt/flop-miner/miner.env` (mode 600) with random 64-character API and session tokens.
3. Pulls the images by digest. This takes a while: about 23 GB.
4. Generates your miner key and prints the **miner address**.
5. Asks Flop's **stake sponsor** for a stake, then waits as long as the sponsor asks between tries. The sponsor can be busy or full: at most 40 uncalibrated miners at a time. Pressing Ctrl+C is safe; continue later with `./flop-miner.sh sponsor`.
6. Downloads the model with Flop's CLI, which checks every file against the on-chain record.
7. Installs cloudflared and the `flop-tunnel` service.

### 4. Lock things down (recommended)

```bash
./flop-miner.sh harden
```

### 5. Start mining

```bash
./flop-miner.sh up
```

It starts the tunnel (a fresh `https://….trycloudflare.com` address each time), loads the model onto the GPU, starts the worker, which publishes your address to Flop's mailbox. `./flop-miner.sh status` shows everything on chain and off.

In the first few hours the network runs **calibration**: a burst of test jobs that measures your GPU and records a cap (GFLOP/s) and a tier on chain. After that, the gateway starts opening sessions with you.

> **Devnet only, here too.** Same warning as on Windows: devnet FLOP has no value, and unanswered network test jobs expire after 600 blocks and **cut your sponsored stake** — run `./flop-miner.sh down` before switching the PC off.

## Daily use

| Shortcut | What it does |
|---|---|
| **FLOP Miner - Start** | Starts the tunnel, backend, worker and dashboard. Closing its window does **not** stop mining. |
| **FLOP Miner - Stop** | Stops everything and shuts the distro down. **Run it before switching the PC off.** |
| **FLOP Miner - Status** | On-chain registration, stake and calibration, plus tunnel, containers, GPU and recent jobs |
| **FLOP Miner - Dashboard** | Opens the live dashboard in your browser |
| **FLOP Miner - Widget** | A compact always-available window (Edge app mode) |

Why stop before switching off: network test jobs that go unanswered expire after 600 blocks and **cut your sponsored stake**. `down` takes you offline cleanly.

Also stop Windows from sleeping while you mine:

```powershell
powercfg /change standby-timeout-ac 0
```

The screen can still turn off.

On native Linux there are no Desktop shortcuts: you run `./flop-miner.sh up` in a terminal to start a mining session, and everything keeps running until you run `./flop-miner.sh down` or switch the PC off. **`down` before shutdown still applies** — the dashboard (`./flop-miner.sh status`, or `flop-dash.py` on `127.0.0.1:8650`) tells you within seconds if something stops.

## The dashboard and widget

`flop-dash.py` serves `http://127.0.0.1:8650` from inside the distro, using only the Python standard library. It shows:

- **Status chips**: mining / stopped, tunnel up / down, chain link stable / dropping, mailbox ok, TOPLOC on / off
- **Gateway sessions**: opened, settled, failed, test FLOP earned, and the last error (only while it is newer than your last settlement)
- **Calibration cap** and tier, read from the chain
- **Paid on chain**: settlements and total test FLOP, read from Flop's hosted indexer
- **Test jobs** answered, GPU power, temperature and load, and a 30-minute power and load chart
- **Activity** feed: sessions opened, settled (with amount and tx) and failed, worker restarts, calibration events

Click **🔕 alerts off** to turn alerts on. You get a chime, an on-screen card and a flashing tab title when a session settles or fails, the chain link drops, the worker stops, or calibration changes. For Windows pop-ups too, open the dashboard in a normal Edge tab, click the padlock, set Notifications to Allow, then reload.

## Is it working? Am I getting paid?

It is working when the widget's dot is **green and pulsing**, every chip is green, and "checked Ns ago" keeps updating.

You are paid per **settled gateway session**:

- Sessions arrive every 20–45 minutes. Routing is decided by Flop's gateway and is uneven between miners.
- Each one is small. On this miner: 16–205 GFLOP of work, paying **0.10–1.25 test FLOP**, settled cooperatively about 4 minutes after it opened. Now and then a session closes without any work and pays nothing.
- Test jobs and calibration pay nothing. They prove your GPU and set your cap.
- The **1,000,000 FLOP stake** is the sponsor's, delegated to you. It is not income.

Check payments in three places:

- the dashboard's **Paid on chain** card
- the explorer: `https://explorer.devnet.flop.network/account/<your miner address>`
- the [devnet miner board](https://flop-devnet-board.vercel.app), which lists every miner that got channels and pay

## Settings

All settings are kept in `/opt/flop-miner/miner.env` and survive restarts.

| Command | Default | Use |
|---|---|---|
| `./flop-miner.sh commitids on\|off` | **on** | `SESSION_COMMIT_IDS=1`. Flop Labs' CTO gave this fix on 8 Nov 2026 for sessions failing with "V2 leaf cannot bind a TOPLOC activation commitment". It should become unnecessary in a later release. |
| `./flop-miner.sh toploc on\|off` | **on** | The TOPLOC activation proof. Keep it **on**: backend v0.1.239 rejects requests without it ("toploc binding required"). |
| `GPU_UTIL=0.5 ./flop-miner.sh setup` | 0.6 | Share of VRAM the backend reserves. Lower it if the backend runs out of memory or you want VRAM for other apps. |
| `WORKER_NAME=… ./flop-miner.sh setup` | `flop-pc-miner` | Your public worker label |

Run them like this:

```powershell
wsl -d flop-miner --cd ~ -- bash -lc "./flop-miner.sh commitids"
```

With no `on|off`, a command prints the current value. With `on|off`, it saves the value and restarts only the worker, and only if the miner is fully running. Otherwise the value applies at the next Start.

## Troubleshooting

Every one of these happened on the test PC.

| Symptom | Cause | Fix |
|---|---|---|
| `run this inside the flop-miner WSL distro` | A plain `wsl` opened your default distro | Always use `wsl -d flop-miner` |
| Sponsor: `503`, `signer balance too low`, `auto_miner_cap_full` | The sponsor is down, out of funds, or full (40 uncalibrated miners max) | Nothing; the script waits `retryAfterSeconds` and retries. Or run `./flop-miner.sh sponsor` later |
| `MODEL_VERIFICATION_FAILED` | The CLI in `releases.json` is older than the chain runtime | Already handled: the script uses the official CLI v0.1.290 or newer from the same org |
| Calibration `No response received from RPC endpoint`, chain link dropping, `1006` every ~13 s | WSL's link MTU (1280 on some PCs) is smaller than Docker's 1500 | Already handled: `up` matches the miner network's MTU to `eth0` |
| Session error `V2 leaf cannot bind a TOPLOC activation commitment (fail closed)` | worker v0.1.286 + backend v0.1.239 | `./flop-miner.sh commitids on` (the default since v1.0.0) |
| `Inference (openai) error 500` / backend log `toploc binding required` | TOPLOC was switched off | `./flop-miner.sh toploc on` |
| **tunnel down** chip | The tunnel was stopped, for example after a partial restart | Double-click Start (re-runs `up` with a fresh tunnel address) |
| A gap of hours with no sessions | Either routing on Flop's side or the PC slept | Check that Windows didn't sleep (see the command after this table). If it didn't, it's routing; nothing to fix |
| A few `calibration check failed … in 60s` lines | The public devnet RPC is slow for a moment | Ignore it unless it lasts over 15 minutes |
| `\\wsl$` or `\\wsl.localhost` paths don't open | Some PCs don't expose the WSL share | Not needed: `install.ps1` copies files through a pipe |
| A PowerShell one-liner behaves strangely | PowerShell expands `$(...)`, and 5.1 strips nested double quotes, before WSL sees the command | Wrap Linux commands in **single quotes**: `wsl -d flop-miner -- bash -lc '...'` |
| The alerts button does nothing | The widget window has no address bar, so Edge hides the permission prompt | The chime and on-screen alerts still work. For Windows pop-ups, allow notifications from a normal Edge tab |

Did Windows sleep in the last 14 hours? (No output means no.)

```powershell
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power','Microsoft-Windows-Power-Troubleshooter'; Id=42,107,1; StartTime=(Get-Date).AddHours(-14)} -ErrorAction SilentlyContinue | Select-Object TimeCreated, Id
```

Worker session lines from the last hour:

```powershell
wsl -d flop-miner -- bash -lc 'docker logs --since 60m flop-miner-worker-1 2>&1 | grep -i -e session -e error -e settle | tail -n 30'
```

## Updating and uninstalling

**New Flop release.** When Flop publishes new images in `releases.json`, re-run setup and then Start. Setup is safe to repeat: it keeps your key, worker name and GPU setting, skips the sponsor if you are already registered, and pulls the new images by digest.

```powershell
wsl -d flop-miner --cd ~ -- ./flop-miner.sh setup
```

**New version of this repo.** Download it again (step 2) and re-run `install.ps1`.

**Uninstall.** This deletes the distro, including the miner key and everything in it:

```powershell
wsl -d flop-miner --cd ~ -- ./flop-miner.sh down
wsl --unregister flop-miner
Remove-Item "$([Environment]::GetFolderPath('Desktop'))\FLOP Miner - *.cmd"
```

## FAQ

**Can I game while it mines?** The backend keeps about 60% of your VRAM reserved. Light use is fine. For a heavy game, click Stop first and Start afterwards.

**Does it keep running if I close every window?** Yes. It runs until you click Stop or Windows shuts down. The dashboard tells you within seconds if something stops.

**Why WSL and not Docker Desktop?** Docker Engine inside a dedicated distro is smaller, open source, has no licence questions, and keeps the miner isolated from your everyday tools.

**Is my main wallet at risk?** No. The miner uses a brand-new key that only ever holds sponsored devnet stake and test FLOP. Nothing in this repo asks for, reads or sends any other key.

**Why is my calibration cap low?** The cap is measured during the calibration burst, and connection trouble during that burst lowers it. The test PC's 28 GFLOP/s came from a run disturbed by the MTU problem (see Troubleshooting), before the fix. Flop may re-measure miners later.

## Credits

- [Flop Labs](https://flop.finance) for the network, the official images and [`releases.json`](https://flop.network/releases.json), and to Flop's CTO for the `SESSION_COMMIT_IDS` fix within hours of the report.
- The compose layout follows the community guide [UfukNode/FLOP-Miner-Kurulum](https://github.com/UfukNode/FLOP-Miner-Kurulum), reviewed line by line.
- Related, by the same author: [flop-devnet-starter](https://github.com/Makabeez/flop-devnet-starter) (agent side: key, faucet, gateway chat) and [flop-devnet-board](https://github.com/Makabeez/flop-devnet-board) (a live board of every devnet miner).

## License

[MIT](LICENSE). Use at your own risk; this is devnet software around third-party images.
