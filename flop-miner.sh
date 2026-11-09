#!/usr/bin/env bash
# flop-miner.sh v2.0.0 — FLOP devnet GPU miner on a native Linux PC with an NVIDIA GPU (Ubuntu 24.04-class).
# https://github.com/Makabeez/flop-pc-miner  (MIT). Tested on an RTX 5080 (16 GB), Ubuntu 24.04, Docker Engine.
#
# Trust model (everything below is checked by this script, nothing is taken from third parties):
#   - Container images: only ghcr.io/flop-labs/flop-core/* (the official github.com/flop-labs org),
#     pulled by sha256 digest exactly as listed in Flop's official https://flop.network/releases.json.
#   - Model hash and decode policy: taken from that same official releases.json.
#   - Sponsor request: the documented POST https://faucet.flop.network/sponsor/miner, signed message
#     "flop-sponsor/v1|miner|<genesis>|<address>|<issuedAt>"; only address + signature leave the PC.
#   - System packages: Ubuntu, Docker, NVIDIA and Cloudflare official signed apt repositories.
#   - Exposure: nothing listens on your LAN. The only public entry is the Cloudflare tunnel to the
#     worker's session port (127.0.0.1:8645), which refuses requests without SESSION_AUTH_TOKEN.
#   - Secrets (miner seed, session seed, tokens) stay in /opt/flop-miner, mode 600; seeds root-owned.
#
#   ./flop-miner.sh prereqs   once, first: Docker Engine + NVIDIA Container Toolkit from their official apt repos
#   ./flop-miner.sh setup     once: verify official release list, settings, images, key, sponsor, model, tunnel
#   ./flop-miner.sh harden    once, after setup: audit the miner's exposure (localhost-only ports, firewall)
#   ./flop-miner.sh up        each session: tunnel -> new HTTPS address -> backend -> worker
#   ./flop-miner.sh status    chain status, calibration, tunnel, GPU power, last worker lines
#   ./flop-miner.sh logs      follow the worker log (Ctrl+C leaves the log; the miner keeps running)
#   ./flop-miner.sh sponsor   ask the sponsor (waits as long as it asks), or top up stake if calibration runs short
#   ./flop-miner.sh toploc on|off   switch the optional TOPLOC activation proof (kept across restarts)
#   ./flop-miner.sh commitids on|off   SESSION_COMMIT_IDS (Flop Labs' fix for the V2 leaf + TOPLOC error, 8 Nov 2026)
#   ./flop-miner.sh down      stop worker, backend and tunnel (run before switching the PC off)
#   ./flop-miner.sh version   print this script's version
#
# Devnet only: no rewards, no airdrop weight. Unanswered network test jobs expire after 600 blocks and
# cut the sponsored stake, so run `down` before switching the PC off. Never reuse the miner seed.
set -euo pipefail

D=/opt/flop-miner
RELEASES_URL=https://flop.network/releases.json
SPONSOR_URL=https://faucet.flop.network
RPC=wss://rpc.devnet.flop.network
MAILBOX=https://mailbox.devnet.flop.network
GENESIS=0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631
MODEL_HASH=0x1a52c1abe781db051e70ae357f72bb70161ca0c092df0b618f22a9292ba63306
MODEL_NAME=Qwen/Qwen2.5-0.5B-Instruct
MODEL_SLUG=qwen/qwen2.5-0.5b-instruct
GPU_UTIL_ARG="${GPU_UTIL:-}"; WORKER_NAME_ARG="${WORKER_NAME:-}"   # only override saved values when given
GPU_UTIL="${GPU_UTIL:-0.6}"
WORKER_NAME="${WORKER_NAME:-flop-pc-miner}"   # public label; choose yours: WORKER_NAME=alice-4090 ./flop-miner.sh setup
VERSION=2.0.0
# CLI v0.1.290 reports a verified model without an "ok" field, so the model check also accepts
# verified_root == MODEL_HASH (the root the CLI recomputed from the files, compared with the chain).
# Floor for the CLI image. On 7 Nov 2026 the chain moved to runtime spec 1014014 while releases.json still
# listed cli v0.1.220 (supports up to 1014003), so model verification failed. v0.1.290 is published by the
# same official org (ghcr.io/flop-labs) and lists 1014014 in SUPPORTED_SPEC_VERSIONS. The newer of the two wins.
MIN_CLI=ghcr.io/flop-labs/flop-core/cli:v0.1.290@sha256:24e32b034b0d3600ee28127dad30d4228e623235f2dc62da34c2c85ba5e21a88

die()  { echo "error: $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

: "${USER:=$(id -un)}"   # not always set in non-login shells
[ "$(uname -s)" = Linux ] || die "this is for native Linux (Ubuntu 24.04-class + NVIDIA); this is: $(uname -s)"
need_docker() {
  command -v docker >/dev/null || die "docker is not installed yet: run ./flop-miner.sh prereqs"
  docker info >/dev/null 2>&1 || die "docker needs sudo here: log out and back in (the docker group applies at login), retry"
}

envget()  { grep "^$1=" "$D/miner.env" | head -n 1 | cut -d= -f2-; }
envset()  { if grep -q "^$1=" "$D/miner.env"; then sed -i "s|^$1=.*|$1=$2|" "$D/miner.env"; else echo "$1=$2" >> "$D/miner.env"; fi; }
cli()     { docker run --rm --network host -v "$D:/work" "$(envget FLOP_CLI)" "$@"; }
compose() { docker compose --env-file "$D/miner.env" -f "$D/docker-compose.yml" "$@"; }
address() { [ -f "$D/address" ] || die "no miner key yet: run ./flop-miner.sh setup"; cat "$D/address"; }
backend_ready() {
  compose exec -T backend python3 -c 'import os, urllib.request as u; u.urlopen(u.Request("http://127.0.0.1:8000/health", headers={"Authorization": "Bearer " + os.environ["INFERENCE_API_KEY"]}), timeout=5)' >/dev/null 2>&1
}

write_compose() {
  # Same services as the community guide (UfukNode/FLOP-Miner-Kurulum), reviewed line by line, plus
  # GPU_MEMORY_UTILIZATION for a desktop GPU. Backend: no published port. Worker: localhost-only port.
  # Secrets are mounted read-only. No privileged mode, no Docker socket, no host paths beyond this folder.
  cat > "$D/docker-compose.yml" <<'EOF'
name: flop-miner

services:
  backend:
    image: ${FLOP_BACKEND_IMAGE:?set FLOP_BACKEND_IMAGE in miner.env}
    restart: unless-stopped
    environment:
      MODEL_NAME: ${MODEL_NAME}
      MODEL_HASH: ${MODEL_HASH}
      MODEL_DIR: /models/checkpoint
      GPU_MEMORY_UTILIZATION: ${GPU_MEMORY_UTILIZATION:-0.6}
      TOPLOC_PRECISION: ${TOPLOC_PRECISION}
      DECODE_POLICY: ${DECODE_POLICY}
      DECODE_POLICY_HASH: ${DECODE_POLICY_HASH}
      INFERENCE_API_KEY: ${INFERENCE_API_KEY:?set INFERENCE_API_KEY in miner.env}
    volumes:
      - ./models/qwen2.5-0.5b-instruct:/models/checkpoint:ro
    ipc: host
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

  worker:
    image: ${FLOP_WORKER_IMAGE:?set FLOP_WORKER_IMAGE in miner.env}
    restart: unless-stopped
    depends_on:
      - backend
    command: ["--rpc", "${FLOP_RPC}"]
    working_dir: /data
    environment:
      MINER_BACKEND: openai
      OPENAI_BASE_URL: http://backend:8000/v1
      INFERENCE_API_KEY: ${INFERENCE_API_KEY}
      MINER_MODEL: ${MODEL_NAME}
      OPENROUTER_MODEL_SLUG: ${OPENROUTER_MODEL_SLUG}
      WORKER_NAME: ${WORKER_NAME}
      MINER_SIGNER_FILE: /run/flop/signer
      MINER_ENCLAVE_KEY_FILE: /run/flop/enclave.seed
      MODEL_HASH: ${MODEL_HASH}
      MODEL_PRECISION: ${MODEL_PRECISION}
      MODEL_CHECKPOINT_DIR: /models/checkpoint
      DECODE_POLICY_HASH: ${DECODE_POLICY_HASH}
      TOPLOC_ENABLED: "${TOPLOC_ENABLED:-1}"
      TOPLOC_PRECISION: ${TOPLOC_PRECISION}
      SESSION_PORT: "8645"
      SESSION_CHAIN: "1"
      SESSION_COMMIT_IDS: "${SESSION_COMMIT_IDS:-1}"
      SESSION_AUTH_TOKEN: ${SESSION_AUTH_TOKEN:?set SESSION_AUTH_TOKEN in miner.env}
      SESSION_PUBLIC_HOST: ${SESSION_PUBLIC_HOST}
      GHOST_MAILBOX_URL: ${GHOST_MAILBOX_URL}
      GHOST_KEY_FILE: /data/ghost-x25519.json
    volumes:
      - ./signer:/run/flop/signer:ro
      - ./session-keys/enclave.seed:/run/flop/enclave.seed:ro
      - ./models/qwen2.5-0.5b-instruct:/models/checkpoint:ro
      - worker-data:/data
    ports:
      - "127.0.0.1:8645:8645"

volumes:
  worker-data:

# A VPN or constrained link can run a smaller MTU than Docker's default 1500-byte packets.
# Without matching it, TLS/WebSocket connections from the miner network stall and drop (1006).
networks:
  default:
    driver_opts:
      com.docker.network.driver.mtu: "${NET_MTU:-1500}"
EOF
}

write_sponsor() {
  # Reviewed: signs the documented message with the miner seed and POSTs only
  # {address, issuedAt, signature} to the official sponsor. It never prints or sends the seed,
  # and refuses to sign if the sponsor reports a different chain than the FLOP devnet.
  cat > "$D/sponsor.mjs" <<'EOF'
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire('/app/');
const { Keyring } = require('@polkadot/keyring');
const { cryptoWaitReady } = require('@polkadot/util-crypto');
const SPONSOR = process.env.SPONSOR_URL;
const GENESIS = process.env.EXPECTED_GENESIS;
const seed = readFileSync('/work/signer', 'utf8').trim();
await cryptoWaitReady();
const pair = new Keyring({ type: 'sr25519', ss58Format: 42 }).addFromUri(seed);
const chain = await (await fetch(`${SPONSOR}/chain`)).json();
if (chain.genesisHash !== GENESIS) { console.log(`refusing: sponsor reports genesis ${chain.genesisHash}`); process.exit(1); }
const issuedAt = String(Date.now());
const message = `flop-sponsor/v1|miner|${chain.genesisHash}|${pair.address}|${issuedAt}`;
const signature = `0x${Buffer.from(pair.sign(new Uint8Array(Buffer.from(message, 'utf8')))).toString('hex')}`;
console.log(`Miner address: ${pair.address}`);
const res = await fetch(`${SPONSOR}/sponsor/miner`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ address: pair.address, issuedAt, signature }),
});
console.log(`HTTP ${res.status}`);
console.log(await res.text());
process.exit(res.ok ? 0 : 1);
EOF
  chmod 644 "$D/sponsor.mjs"
}

is_registered() {
  # true when the chain already lists this miner (then the sponsor must not be asked again)
  cli --network devnet --rpc "$RPC" miner status "$(address)" --json 2>/dev/null \
    | jq -e '(.registered // false) | if type == "boolean" then . else (. != null and . != 0) end' >/dev/null 2>&1
}

# Retry the sponsor, waiting as long as it asks (retryAfterSeconds, at least 3 min). Prints the real reason.
sponsor_until_ok() {
  local tries="$1" i out wait reason
  for i in $(seq 1 "$tries"); do
    if out=$(sponsor_once 2>&1); then echo "$out" | tail -n 3; return 0; fi
    reason=$(echo "$out" | grep -o '"error":"[^"]*"' | head -n 1 | cut -d'"' -f4)
    wait=$(echo "$out" | grep -o '"retryAfterSeconds":[0-9]*' | head -n 1 | cut -d: -f2)
    [ "${wait:-0}" -ge 180 ] || wait=180
    echo "$(date '+%H:%M') sponsor refused ($i/$tries): ${reason:-$(echo "$out" | tail -n 1 | cut -c1-160)}"
    [ "$i" -lt "$tries" ] || return 1
    echo "  next try in $((wait / 60)) min (Ctrl+C is safe; nothing is registered yet)"; sleep "$wait"
  done
}

sponsor_once() {
  docker run --rm -v "$D:/work" -e SPONSOR_URL="$SPONSOR_URL" -e EXPECTED_GENESIS="$GENESIS" \
    --entrypoint node "$(envget FLOP_CLI)" /work/sponsor.mjs
}

cmd_prereqs() {
  # Docker Engine (download.docker.com) and NVIDIA Container Toolkit (nvidia.github.io), each from its
  # official signed apt repository, following their install guides.
  grep -q 'Ubuntu' /etc/os-release || die "this expects an Ubuntu 24.04-class host"
  nvidia-smi -L >/dev/null 2>&1 || die "no NVIDIA GPU visible on the host (nvidia-smi -L): install the latest NVIDIA driver"
  step "Docker Engine (official apt repository)"
  sudo apt-get update -qq && sudo apt-get install -y -qq ca-certificates curl gnupg jq openssl >/dev/null
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  local codename; codename=$(sed -n 's/^UBUNTU_CODENAME=//p' /etc/os-release)
  [ -n "$codename" ] || codename=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release)
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  step "NVIDIA Container Toolkit (official apt repository)"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit >/dev/null
  sudo nvidia-ctk runtime configure --runtime=docker >/dev/null
  sudo systemctl restart docker
  sudo usermod -aG docker "$USER"
  step "GPU inside Docker"
  sudo docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L || die "Docker cannot see the GPU (see the guide's troubleshooting)"
  echo
  echo "prereqs done. Log out and back in (the docker group applies at login), then: ./flop-miner.sh setup"
}

cmd_setup() {
  step "packages (Ubuntu official)"
  sudo apt-get update -qq && sudo apt-get install -y -qq jq openssl curl ca-certificates >/dev/null

  step "official release list: $RELEASES_URL"
  local rel cli_img worker_img backend_img policy_hash policy img
  rel=$(curl -fsS --proto '=https' "$RELEASES_URL") || die "cannot read $RELEASES_URL"
  cli_img=$(jq -r '.cli.image' <<<"$rel")
  worker_img=$(jq -r '."miner-worker".image' <<<"$rel")
  backend_img=$(jq -r '."miner-vllm-toploc".image' <<<"$rel")
  for img in "$cli_img" "$worker_img" "$backend_img"; do
    [[ "$img" =~ ^ghcr\.io/flop-labs/flop-core/[a-z0-9-]+:v[0-9.]+@sha256:[0-9a-f]{64}$ ]] \
      || die "release list returned an unexpected image reference: $img"
    echo "  $img"
  done
  jq -e --arg m "$MODEL_HASH" '.models[$m].name' <<<"$rel" >/dev/null || die "model $MODEL_HASH is not in the official release list"
  policy_hash=$(jq -r --arg m "$MODEL_HASH" '.models[$m].decode_policies | keys[0]' <<<"$rel")
  [[ "$policy_hash" =~ ^0x[0-9a-f]{64}$ ]] || die "no decode policy for the model in the release list"
  policy=$(jq -c --arg m "$MODEL_HASH" --arg p "$policy_hash" '.models[$m].decode_policies[$p]' <<<"$rel")
  echo "  model $MODEL_HASH ($MODEL_NAME), decode policy $policy_hash"
  local rel_v min_v
  rel_v=$(sed -E 's/.*:v([0-9.]+)@.*/\1/' <<<"$cli_img"); min_v=$(sed -E 's/.*:v([0-9.]+)@.*/\1/' <<<"$MIN_CLI")
  if [ "$(printf '%s\n%s\n' "$rel_v" "$min_v" | sort -V | tail -n 1)" != "$rel_v" ]; then
    echo "  cli v$rel_v in the release list is older than the chain needs; using official $MIN_CLI"
    cli_img="$MIN_CLI"
  fi

  step "settings in $D"
  sudo install -d -o "$USER" -g "$USER" -m 700 "$D"
  install -d -m 700 "$D/models" "$D/session-keys"
  [ -f "$D/miner.env" ] || install -m 600 /dev/null "$D/miner.env"
  envset FLOP_CLI "$cli_img"
  envset FLOP_WORKER_IMAGE "$worker_img"
  envset FLOP_BACKEND_IMAGE "$backend_img"
  envset FLOP_RPC "$RPC"
  envset GHOST_MAILBOX_URL "$MAILBOX"
  { [ -n "$WORKER_NAME_ARG" ] || ! grep -q "^WORKER_NAME=" "$D/miner.env"; } && envset WORKER_NAME "$WORKER_NAME"
  envset MODEL_NAME "$MODEL_NAME"
  envset OPENROUTER_MODEL_SLUG "$MODEL_SLUG"
  envset MODEL_HASH "$MODEL_HASH"
  envset MODEL_PRECISION BF16
  envset TOPLOC_PRECISION bf16
  envset DECODE_POLICY_HASH "$policy_hash"
  envset DECODE_POLICY "'$policy'"
  { [ -n "$GPU_UTIL_ARG" ] || ! grep -q "^GPU_MEMORY_UTILIZATION=" "$D/miner.env"; } && envset GPU_MEMORY_UTILIZATION "$GPU_UTIL"
  grep -q '^TOPLOC_ENABLED=' "$D/miner.env" || envset TOPLOC_ENABLED 1
  grep -q '^INFERENCE_API_KEY=[0-9a-f]\{64\}$' "$D/miner.env" || envset INFERENCE_API_KEY "$(openssl rand -hex 32)"
  grep -q '^SESSION_AUTH_TOKEN=[0-9a-f]\{64\}$' "$D/miner.env" || envset SESSION_AUTH_TOKEN "$(openssl rand -hex 32)"
  grep -q '^SESSION_PUBLIC_HOST=' "$D/miner.env" || envset SESSION_PUBLIC_HOST ""
  chmod 600 "$D/miner.env"
  write_compose
  write_sponsor
  compose config -q || die "docker-compose.yml did not validate"

  step "images by digest from ghcr.io/flop-labs (backend is ~23 GB)"
  docker pull -q "$cli_img"
  compose pull -q

  step "miner key (new devnet-only account)"
  if ! sudo test -s "$D/signer"; then
    local tmp; tmp=$(mktemp "$D/.key.XXXXXX"); chmod 600 "$tmp"
    cli keys generate --type sr25519 --show-secret --json > "$tmp"
    (umask 077; jq -r .mnemonic "$tmp" > "$D/signer"; jq -r .address "$tmp" > "$D/address")
    shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
    [ -s "$D/address" ] || die "key generation failed"
  fi
  sudo test -s "$D/session-keys/enclave.seed" || openssl rand -hex 32 | sudo tee "$D/session-keys/enclave.seed" >/dev/null
  # Flop's docs: the worker refuses a signer that is not root-owned with mode 0600.
  sudo chown root:root "$D/signer" "$D/session-keys/enclave.seed"
  sudo chmod 600 "$D/signer" "$D/session-keys/enclave.seed"
  echo "miner address: $(address)"

  step "stake sponsor (official faucet.flop.network)"
  if is_registered; then
    echo "already registered on chain: skipping the sponsor"
  else
    sponsor_until_ok "${SPONSOR_TRIES:-48}" || die "sponsor still refusing; run ./flop-miner.sh sponsor later, then ./flop-miner.sh setup"
  fi

  step "model (the CLI checks every file against the on-chain record)"
  local i
  for i in 1 2 3; do
    if cli --network devnet --rpc "$RPC" model fetch "$MODEL_HASH" --precision BF16 \
         --output-dir /work/models/qwen2.5-0.5b-instruct --json \
         | jq -e --arg r "${MODEL_HASH#0x}" '(.ok == true) or (.verified_root == $r)' >/dev/null; then echo "model ok (verified root matches the chain)"; break; fi
    [ "$i" -lt 3 ] || die "model fetch failed 3 times"
    echo "model fetch failed; retrying"; sleep 10
  done

  step "tunnel (cloudflared from Cloudflare's signed apt repository)"
  if ! command -v cloudflared >/dev/null; then
    sudo install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
      | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    sudo apt-get update -qq && sudo apt-get install -y -qq cloudflared >/dev/null
  fi
  sudo tee /etc/systemd/system/flop-tunnel.service >/dev/null <<'EOF'
[Unit]
Description=FLOP miner HTTPS tunnel (forwards only to the worker session port)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8645
Restart=always
RestartSec=5
DynamicUser=yes
StateDirectory=flop-tunnel
Environment=HOME=/var/lib/flop-tunnel
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
EOF
  sudo systemctl daemon-reload
  echo
  echo "setup done. Next: ./flop-miner.sh harden, then ./flop-miner.sh up"
}

cmd_harden() {
  # Native posture check: nothing may be published beyond loopback; the only public entry is the
  # outbound Cloudflare tunnel. Prints what is listening and how to lock the firewall down.
  step "docker published ports (every mapping must be 127.0.0.1)"
  local ports; ports=$(docker ps --format '{{.Names}}\t{{.Ports}}')
  if [ -n "$ports" ]; then echo "$ports" | sed 's/^/  /'; else echo "  no containers running (./flop-miner.sh up starts them)"; fi
  [ -z "$(docker ps --format '{{.Ports}}' | grep -- '->' | grep -v '^127\.0\.0\.1:' || true)" ] \
    || die "a container publishes a port beyond 127.0.0.1: fix the ports: section in $D/docker-compose.yml"
  step "host listeners (worker 8645, dashboard 8650)"
  local listen; listen=$(ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -E ':(8645|8650)$' || true)
  if [ -n "$listen" ]; then echo "$listen" | sed 's/^/  /'; else echo "  neither the worker nor the dashboard is listening yet"; fi
  [ -z "$(printf '%s\n' "$listen" | grep -v '^127\.0\.0\.1:' || true)" ] \
    || die "a miner port listens beyond 127.0.0.1: $listen"
  step "firewall"
  echo "  the miner needs no inbound ports: the tunnel is an outbound connection."
  if command -v ufw >/dev/null; then
    sudo ufw status | head -n 1
    echo "  recommended: sudo ufw default deny incoming && sudo ufw enable"
  else
    echo "  ufw not installed; ensure no inbound rule allows 8645/8650, or: sudo ufw default deny incoming && sudo ufw enable"
  fi
  echo
  echo "harden done: worker and dashboard must stay on 127.0.0.1; rerun this after any compose change."
}

cmd_up() {
  address >/dev/null
  # Match the miner network's packet size to the default-route interface's MTU when it is below
  # Docker's default (1500), and refresh the compose file.
  local ifc mtu
  ifc=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
  mtu=1500
  [ -z "$ifc" ] || mtu=$(cat "/sys/class/net/$ifc/mtu" 2>/dev/null || echo 1500)
  [ "$mtu" -lt 1500 ] || mtu=1500
  envset NET_MTU "$mtu"
  grep -q '^SESSION_COMMIT_IDS=' "$D/miner.env" || envset SESSION_COMMIT_IDS 1
  write_compose
  compose config -q || die "docker-compose.yml did not validate"
  if docker network inspect flop-miner_default >/dev/null 2>&1 && \
     [ "$(docker network inspect flop-miner_default -f '{{index .Options "com.docker.network.driver.mtu"}}')" != "$(envget NET_MTU)" ]; then
    echo "recreating the miner network with MTU $(envget NET_MTU)"
    compose down
  fi
  step "tunnel"
  local since host i signer
  since=$(date '+%Y-%m-%d %H:%M:%S')
  sudo systemctl restart flop-tunnel
  for i in $(seq 1 30); do
    host=$(sudo journalctl -u flop-tunnel --since "$since" --no-pager | grep -o '[a-z0-9-]*\.trycloudflare\.com' | tail -n 1 || true)
    [ -n "$host" ] && break; sleep 2
  done
  [ -n "$host" ] || die "no tunnel address after 60 s: sudo journalctl -u flop-tunnel -n 30"
  envset SESSION_PUBLIC_HOST "$host"
  echo "public address: https://$host (answers 401 without the session token)"

  step "backend (model on the GPU)"
  compose up -d backend
  for i in $(seq 1 120); do
    backend_ready && break
    if compose logs --since 15m backend 2>/dev/null | grep -qiE 'out of memory|less than desired GPU memory|Traceback'; then
      compose logs --tail 25 backend; die "backend failed to start (see above); try: GPU_UTIL=0.5 ./flop-miner.sh setup"
    fi
    sleep 5
  done
  backend_ready || die "backend not ready after 10 min: ./flop-miner.sh status"
  echo "backend ready"

  step "worker"
  compose up -d --force-recreate worker
  sleep 20
  compose logs --tail 40 worker | grep -E 'Signer:|model-gate|Session host|Endpoint ad|answered task|calibration|rror' || true
  signer=$(compose logs worker | grep -o 'Signer: [0-9A-Za-z]*' | tail -n 1 | cut -d' ' -f2)
  if [ -n "$signer" ] && [ "$signer" != "$(address)" ]; then
    compose stop worker; die "worker signed as $signer, not your miner $(address); stopped it"
  fi
  echo
  echo "mining. You can close this window: the miner keeps running until ./flop-miner.sh down."
}

cmd_status() {
  local a; a=$(address)
  step "miner $a"
  cli --network devnet --rpc "$RPC" miner status "$a" --json | jq '{registered, stake, calibration}' || true
  echo "explorer: https://explorer.devnet.flop.network/account/$a"
  echo "board:    https://flop-devnet-board.vercel.app"
  step "TOPLOC proof"; [ "$(envget TOPLOC_ENABLED)" = 0 ] && echo "OFF (plain V2 settlements)" || echo "ON"
  step "session commit ids"; [ "$(envget SESSION_COMMIT_IDS)" = 0 ] && echo "OFF" || echo "ON"
  step "tunnel"; echo "https://$(envget SESSION_PUBLIC_HOST)  ($(systemctl is-active flop-tunnel || true))"
  step "containers"; compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
  step "GPU"; nvidia-smi --query-gpu=power.draw,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv
  step "worker (last test jobs / calibration)"; compose logs worker 2>/dev/null | grep -E 'answered task|calibration' | tail -n 5 || true
}

# Rewrite the compose file and restart only the worker, and only if the miner is already fully running:
# starting it from here would skip the tunnel (that is `up`'s job).
apply_worker_setting() {
  write_compose
  compose config -q || die "docker-compose.yml did not validate"
  if ! compose ps --status running --services 2>/dev/null | grep -qx worker \
     || ! systemctl is-active -q flop-tunnel; then
    echo "Saved. The miner is not fully running, so nothing was restarted: it applies at the next ./flop-miner.sh up."
    return
  fi
  compose up -d --no-deps --force-recreate worker
  sleep 25
  compose logs --since 1m worker | grep -E 'Signer:|TOPLOC lanes|Endpoint ad' || true
}

# TOPLOC proof on/off. Off = the worker's "TOPLOC lanes: disabled" mode. Note: backend v0.1.239 refuses
# requests without a TOPLOC binding ("toploc binding required"), so keep it ON with that backend.
cmd_toploc() {
  case "${1:-}" in
    on)  envset TOPLOC_ENABLED 1 ;;
    off) envset TOPLOC_ENABLED 0 ;;
    *)   echo "TOPLOC proof is $( [ "$(envget TOPLOC_ENABLED)" = 0 ] && echo OFF || echo ON ). Use: ./flop-miner.sh toploc on|off"; return ;;
  esac
  apply_worker_setting
}

# SESSION_COMMIT_IDS: official worker flag that lets the V2 settlement leaf carry the token-id commitment
# the TOPLOC binding needs. Flop Labs' CTO advised it on 8 Nov 2026 until the next release fixes it.
cmd_commitids() {
  case "${1:-}" in
    on)  envset SESSION_COMMIT_IDS 1 ;;
    off) envset SESSION_COMMIT_IDS 0 ;;
    *)   echo "SESSION_COMMIT_IDS is $( [ "$(envget SESSION_COMMIT_IDS)" = 0 ] && echo OFF || echo ON ). Use: ./flop-miner.sh commitids on|off"; return ;;
  esac
  apply_worker_setting
  compose exec -T worker printenv SESSION_COMMIT_IDS 2>/dev/null | sed 's/^/worker sees SESSION_COMMIT_IDS=/' || true
}

cmd_sponsor() { if is_registered; then echo "already registered; asking for a stake top-up"; fi; sponsor_until_ok "${SPONSOR_TRIES:-48}"; }
cmd_logs()    { compose logs -f --tail 50 worker; }
cmd_down() {
  compose stop worker backend || true
  sudo systemctl stop flop-tunnel || true
  echo "stopped. Start again with: ./flop-miner.sh up"
}

case "${1:-}" in
  prereqs) cmd_prereqs; exit ;;
  version) echo "flop-miner.sh $VERSION"; exit ;;
  setup|harden|up|status|logs|sponsor|toploc|commitids|down) ;;
  *)       sed -n '2,29p' "$0"; exit 1 ;;
esac
need_docker
case "${1:-}" in
  setup)   cmd_setup ;;
  harden)  cmd_harden ;;
  up)      cmd_up ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  sponsor) cmd_sponsor ;;
  toploc)  shift; cmd_toploc "${1:-}" ;;
  commitids) shift; cmd_commitids "${1:-}" ;;
  down)    cmd_down ;;
  *)       sed -n '2,29p' "$0"; exit 1 ;;
esac
