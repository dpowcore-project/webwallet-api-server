# Bitweb API Server

REST + WebSocket backend for the Bitweb wallet. Bridges HTTP/WS clients to an
[ElectrumX](https://github.com/spesmilo/electrumx) node over TLS, providing
address balances, UTXO sets, transaction history, and real-time push
notifications via Socket.IO.

---

## Architecture

```
Browser / Wallet
      │  REST + Socket.IO
      ▼
  Nginx (TLS termination, rate limiting)
      │  HTTP proxy
      ▼
  Gunicorn + gevent  (app.py → server/)
      │  ElectrumX JSON-RPC over TLS
      ▼
  ElectrumX node  (electrumx.example.com:20002)
```

The server maintains:
- **Connection pool** (`ElectrumPool`) — N parallel persistent TLS connections to ElectrumX, used for all REST calls.
- **Subscriber** (`ElectrumSubscriber`) — one dedicated persistent connection for server-push notifications (new blocks, scripthash changes).
- **In-memory caches** — coinbase cache, TX cache, history cache, tip-height cache; all bounded and self-evicting.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/info` | Current block height |
| `GET` | `/balance/<address>` | Confirmed + unconfirmed balance (satoshis) |
| `GET` | `/unspent/<address>` | UTXO list. Optional: `?amount=<min_sat>` `?confirmed=true` |
| `GET` | `/history/<address>` | Last N txs with direction/amount. Optional: `?limit=<n>` (max 50) |
| `GET` | `/tx/<txid>` | Verbose transaction (vout includes `value_sat`) |
| `GET` | `/rawtx/<txid>` | Raw transaction hex |
| `GET` | `/fee` | Fixed fee rate (satoshis) |
| `POST` | `/broadcast` | Broadcast raw tx hex (form field `raw` or raw body) |

### Response envelope

```json
{ "error": null,  "id": "api-server", "result": { ... } }
{ "error": { "code": 400, "message": "..." }, "id": "api-server", "result": null }
```

### WebSocket (Socket.IO)

Connect to `wss://api.example.com/socket.io/`.

**Client → Server**

```js
socket.emit("subscribe", { address: "WxxxYour address here" })
```

**Server → Client**

| Event | Payload |
|-------|---------|
| `subscribed` | `{ address }` |
| `balance_changed` | `{ balance, confirmed, unconfirmed, pending_out, utxos[], height }` |
| `block` | `{ height }` |
| `error` | `{ message }` |

---

## Requirements

### System

- **OS**: Ubuntu 22.04 / 24.04 LTS (or any systemd-based Linux)
- **Python**: 3.10 or newer
- **Nginx**: 1.18+ (for TLS termination and rate limiting)
- **Certbot**: for Let's Encrypt certificates (optional but recommended)

### Python packages

Listed in `requirements.txt`:

```
flask
flask-cors
flask-socketio
gevent
gevent-websocket
bech32
base58
gunicorn
python-dotenv
```

---

## Install

### 1. Create a dedicated user

```bash
sudo useradd -r -m -s /bin/bash bitweb
```

### 2. Clone / copy the project

```bash
sudo mkdir -p /opt/bitweb-api
sudo cp -r . /opt/bitweb-api/
sudo chown -R bitweb:bitweb /opt/bitweb-api
```

### 3. Create a virtual environment and install dependencies

```bash
  cd /opt/bitweb-api
  python3 -m venv venv
  venv/bin/pip install --upgrade pip
  venv/bin/pip install -r requirements.txt
```

### 4. Configure environment variables

Copy the example and edit:

```bash
sudo cp /opt/bitweb-api/env.example /opt/bitweb-api/.env
sudo nano /opt/bitweb-api/.env
sudo chown bitweb:bitweb /opt/bitweb-api/.env
sudo chmod 600 /opt/bitweb-api/.env
```

See [Configuration](#configuration) for all available variables.

### 5. Create the systemd service

```bash
sudo nano /etc/systemd/system/bitweb-api.service
```

```ini
[Unit]
Description=Bitweb API Server
After=network.target

[Service]
User=bitweb
Group=bitweb
WorkingDirectory=/opt/bitweb-api
EnvironmentFile=/opt/bitweb-api/.env
Environment="PATH=/opt/bitweb-api/venv/bin"
ExecStart=/opt/bitweb-api/venv/bin/gunicorn \
    --bind 127.0.0.1:21223 \
    --worker-class gevent \
    --workers 1 \
    --timeout 0 \
    --keep-alive 75 \
    app:app
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bitweb-api
sudo systemctl status bitweb-api
```

### 6. Configure Nginx

See the included `api2.bitwebcore.example` for a production-ready Nginx config.
Replace `api.example.com` with your domain.

```bash
sudo cp api2.bitwebcore.example /etc/nginx/conf.d/bitweb-api.conf
# edit domain
sudo nginx -t && sudo systemctl reload nginx
```

### 7. Obtain a TLS certificate (Let's Encrypt)

```bash
sudo certbot --nginx -d your.domain.com
```

---

## Configuration

All settings are read from a `.env` file in the project root (via `python-dotenv`).

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | `change-me-in-production` | Flask session secret — **change this** |
| `HOST` | `0.0.0.0` | Bind address (Gunicorn listens here) |
| `PORT` | `21223` | Bind port |
| `DEBUG` | `false` | Flask debug mode — **never enable in production** |
| `ELECTRUM_HOST` | `electrumx.example.com` | ElectrumX hostname |
| `ELECTRUM_PORT` | `20002` | ElectrumX TLS port |
| `ELECTRUM_TIMEOUT` | `15` | Per-call timeout in seconds |
| `ELECTRUM_VERIFY_SSL` | `true` | Set `false` only for self-signed certs |
| `ELECTRUM_POOL_SIZE` | `4` | Number of parallel ElectrumX connections |
| `FIXED_FEE_SATOSHIS` | `10000` | Reported fee rate from `/fee` |

### Example `.env`

```dotenv
SECRET_KEY=replace-with-a-long-random-string

HOST=0.0.0.0
PORT=21223
DEBUG=false

ELECTRUM_HOST=electrumx.example.com
ELECTRUM_PORT=20002
ELECTRUM_TIMEOUT=15
ELECTRUM_VERIFY_SSL=true
ELECTRUM_POOL_SIZE=4

FIXED_FEE_SATOSHIS=10000
```

> **Security note:** `.env` contains secrets. It is owned by the service user
> (`bitweb`) with mode `600` and must never be committed to version control.
> Add `.env` to `.gitignore`.

---

## Running

### Via systemd (production)

```bash
sudo systemctl start bitweb-api
sudo systemctl stop bitweb-api
sudo systemctl restart bitweb-api
sudo systemctl status bitweb-api
```

### Logs

```bash
sudo journalctl -u bitweb-api -f          # follow live
sudo journalctl -u bitweb-api -n 100      # last 100 lines
sudo journalctl -u bitweb-api --since today
```

### Development (local)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp env.example .env   # edit as needed
python3 app.py
```

---

## Tests

```bash
python3 test_address.py
```

Tests cover base58check decoding, all version bytes (mainnet/testnet P2PKH and
P2SH), bech32 P2WPKH/P2WSH with the `web` HRP, and script-hash derivation.

---

## Project Structure

```
.
├── app.py              Entry point (gevent monkey-patch + gunicorn target)
├── requirements.txt    Python dependencies
├── env.example         Configuration template
├── server/
│   ├── __init__.py     Flask app factory, SocketIO init
│   ├── rest.py         All REST routes + WebSocket event handlers
│   ├── electrum.py     ElectrumX client / pool / subscriber
│   ├── address.py      Address → scriptPubKey / scriptHash conversion
│   ├── segwit_addr.py  Bech32 / Bech32m reference implementation
│   └── utils.py        JSON response helpers
├── test_address.py     Unit tests for address.py
└── api.example_nginx  Production Nginx config example
```

---

## License

MIT — see `LICENSE.md`.
