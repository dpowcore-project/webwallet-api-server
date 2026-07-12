"""
REST API blueprint.

Endpoints
---------
GET  /info                     Current block height
GET  /balance/<address>        Confirmed + unconfirmed balance (satoshis)
GET  /unspent/<address>        UTXO list; optional ?amount=<min> ?confirmed=true
GET  /fee                      Fixed fee rate (satoshis)
GET  /tx/<txid>                Verbose transaction (vout includes value_sat)
GET  /history/<address>        List of {tx_hash, height}; height==0 = mempool
POST /broadcast                Broadcast raw transaction hex
"""

import logging
import os
import gevent
import gevent.pool
import re
import threading
import time

from flask import Blueprint, jsonify, request
from flask_cors import cross_origin
from flask_socketio import join_room, leave_room

from server.electrum import ElectrumPool, ElectrumSubscriber
from server.address  import address_to_scripthash, address_to_scriptpubkey
from server          import utils, socketio

log = logging.getLogger(__name__)

def _env_int(key, default):
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default

def _env_bool(key, default):
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default

_ELECTRUM_HOST      = os.environ.get("ELECTRUM_HOST",    "electrumx.example.com")
_ELECTRUM_PORT      = _env_int("ELECTRUM_PORT",           20002)
_ELECTRUM_TIMEOUT   = _env_int("ELECTRUM_TIMEOUT",        15)
_ELECTRUM_VERIFY_SSL = _env_bool("ELECTRUM_VERIFY_SSL",   True)
_ELECTRUM_POOL_SIZE = _env_int("ELECTRUM_POOL_SIZE",      4)
_FIXED_FEE_SATOSHIS = _env_int("FIXED_FEE_SATOSHIS",      10000)

# Module-level state  -  intentionally private (_) to prevent accidental import

_pool = ElectrumPool(
    host       = _ELECTRUM_HOST,
    port       = _ELECTRUM_PORT,
    timeout    = _ELECTRUM_TIMEOUT,
    verify_ssl = _ELECTRUM_VERIFY_SSL,
    size       = _ELECTRUM_POOL_SIZE,
)

_subscriber = ElectrumSubscriber(
    host       = _ELECTRUM_HOST,
    port       = _ELECTRUM_PORT,
    timeout    = _ELECTRUM_TIMEOUT,
    verify_ssl = _ELECTRUM_VERIFY_SSL,
)

bp = Blueprint("api", __name__)

# Coinbase cache {txid: bool}  -  immutable once mined, never invalidated.
# Bounded to MAX_COINBASE_CACHE_SIZE; oldest half evicted when full (~20 MB max).
_coinbase_cache: dict[str, bool] = {}
_coinbase_lock  = threading.Lock()
MAX_COINBASE_CACHE_SIZE = 100_000

# Tip height cache  -  populated by on_new_block, TTL-refreshed on cache miss.
_tip_cache: dict = {}  # {"height": int, "expires": float}
_tip_lock  = threading.Lock()
_TIP_TTL   = 5.0       # seconds

# Verbose TX cache  -  confirmed TXs are immutable, cached indefinitely.
# Bounded to MAX_TX_CACHE_SIZE; oldest half evicted when full.
_tx_cache: dict[str, dict] = {}
_tx_cache_lock = threading.Lock()
MAX_TX_CACHE_SIZE = 50_000

# History cache {scripthash: raw_history_list}.
# Eliminates the dominant source of pool exhaustion under load  -  without it
# every /history request hits ElectrumX even when nothing changed.
# Invalidated in on_scripthash_change().
_history_cache: dict[str, list] = {}
_history_cache_lock = threading.Lock()
MAX_HISTORY_CACHE_SIZE = 50_000

# WebSocket state
_sid_rooms:           dict = {}  # sid -> currently subscribed scripthash
_sid_last_sub:        dict = {}  # sid -> monotonic timestamp of last subscribe
_scripthash_scripts:  dict = {}  # scripthash -> script hex
_scripthash_refcount: dict = {}  # scripthash -> number of subscribed sids
_sid_lock = threading.Lock()

_SUB_MIN_INTERVAL = 1.0  # seconds between subscribe calls per connection

# Validation regexes
_RE_TXID    = re.compile(r'^[0-9a-fA-F]{64}$')
_RE_ADDRESS = re.compile(r'^[a-zA-Z0-9]{25,90}$')
_RE_HEX     = re.compile(r'^[0-9a-fA-F]+$')
_BROADCAST_MAX_BYTES = 100_000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def get_tip_height() -> int:
    """
    Return current chain tip height.

    Reads from _tip_cache when fresh; falls back to blockchain.headers.subscribe
    on cache miss. Thread-safe via double-checked locking on _tip_lock.
    """
    now = time.monotonic()
    if _tip_cache.get("height") is not None and now < _tip_cache.get("expires", 0):
        return _tip_cache["height"]

    with _tip_lock:
        now = time.monotonic()
        if _tip_cache.get("height") is not None and now < _tip_cache.get("expires", 0):
            return _tip_cache["height"]

        tip = _pool.call("blockchain.headers.subscribe")
        _tip_cache["height"]  = tip["height"]
        _tip_cache["expires"] = time.monotonic() + _TIP_TTL
        return _tip_cache["height"]


def is_coinbase_tx(txid: str) -> bool:
    """
    Return True if txid is a coinbase transaction.
    Uses in-memory cache  -  fetches full TX at most once per txid.
    """
    with _coinbase_lock:
        if txid in _coinbase_cache:
            return _coinbase_cache[txid]

    try:
        raw    = _pool.call("blockchain.transaction.get", txid, True)
        vin    = raw.get("vin", [])
        result = bool(vin and "coinbase" in vin[0])
    except Exception:
        return False

    with _coinbase_lock:
        if txid not in _coinbase_cache:
            if len(_coinbase_cache) >= MAX_COINBASE_CACHE_SIZE:
                evict_count = MAX_COINBASE_CACHE_SIZE // 2
                for key in list(_coinbase_cache.keys())[:evict_count]:
                    del _coinbase_cache[key]
                log.info("Coinbase cache evicted %d entries (was full)", evict_count)
            _coinbase_cache[txid] = result
    return result


def fetch_verbose_tx(txid: str) -> dict:
    """
    Fetch verbose TX from ElectrumX, serving from cache when available.
    Only confirmed TXs (with blockhash/blocktime) are cached.
    """
    with _tx_cache_lock:
        if txid in _tx_cache:
            return _tx_cache[txid]

    raw = _pool.call("blockchain.transaction.get", txid, True)

    is_confirmed = bool(raw.get("blockhash") or raw.get("blocktime") or raw.get("confirmations", 0) > 0)
    if is_confirmed:
        with _tx_cache_lock:
            if txid not in _tx_cache:
                if len(_tx_cache) >= MAX_TX_CACHE_SIZE:
                    for key in list(_tx_cache.keys())[:MAX_TX_CACHE_SIZE // 2]:
                        del _tx_cache[key]
                    log.info("TX cache evicted %d entries", MAX_TX_CACHE_SIZE // 2)
                _tx_cache[txid] = raw
    return raw


def fetch_verbose_tx_batch(txids: list) -> dict:
    """
    Fetch multiple verbose TXs in one pipelined batch, serving cache hits first.

    Returns {txid: raw_dict | None}. None means the fetch failed or ElectrumX
    reported an error. Confirmed TXs are written back to _tx_cache.
    """
    result = {}
    needed = []

    with _tx_cache_lock:
        for txid in txids:
            if txid in _tx_cache:
                result[txid] = _tx_cache[txid]
            else:
                needed.append(txid)

    if not needed:
        return result

    requests = [("blockchain.transaction.get", [txid, True]) for txid in needed]
    try:
        responses = _pool.call_batch(requests)
    except Exception as exc:
        log.warning("Batch TX fetch failed: %s", exc)
        for txid in needed:
            result[txid] = None
        return result

    to_cache = {}
    for txid, raw in zip(needed, responses):
        result[txid] = raw
        if raw is None:
            continue
        is_confirmed = bool(
            raw.get("blockhash") or raw.get("blocktime") or raw.get("confirmations", 0) > 0
        )
        if is_confirmed:
            to_cache[txid] = raw

    if to_cache:
        with _tx_cache_lock:
            for txid, raw in to_cache.items():
                if txid not in _tx_cache:
                    if len(_tx_cache) >= MAX_TX_CACHE_SIZE:
                        for key in list(_tx_cache.keys())[:MAX_TX_CACHE_SIZE // 2]:
                            del _tx_cache[key]
                        log.info("TX cache evicted %d entries", MAX_TX_CACHE_SIZE // 2)
                    _tx_cache[txid] = raw

    return result


def extract_vout_address(vout_entry: dict) -> str | None:
    """Extract address string from a verbose vout entry (handles old/new ElectrumX)."""
    spk  = vout_entry.get("scriptPubKey", {})
    addr = spk.get("address")
    if addr:
        return str(addr)
    addrs = spk.get("addresses")
    if addrs and isinstance(addrs, list) and addrs:
        return str(addrs[0])
    return None


def vout_to_satoshis(vout_entry: dict) -> int:
    """Convert a vout value (float BTC from ElectrumX verbose TX) to satoshis.
    Uses string parsing to avoid float64 precision loss at large amounts (>33M BTE).
    Falls back to round() for edge cases like scientific notation (e.g. 1e-08).
    """
    raw = vout_entry.get("value", 0)
    val = str(raw)
    if '.' in val and 'e' not in val.lower():
        ip, fp = val.split('.', 1)
        fp = fp[:8].ljust(8, '0')
        return int(ip) * 100_000_000 + int(fp)
    return int(round(raw * 1e8))


def validate_address(address: str):
    """Raise ValueError if address looks obviously wrong before hitting ElectrumX."""
    if not address or not _RE_ADDRESS.match(address):
        raise ValueError("Invalid address format  -  expected alphanumeric, 25-90 characters")


def validate_txid(txid: str):
    """Raise ValueError if txid is not exactly 64 hex chars."""
    if not txid or not _RE_TXID.match(txid):
        raise ValueError("Invalid txid  -  expected 64 hex characters")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/info", methods=["GET"])
@cross_origin()
def get_info():
    try:
        height = get_tip_height()
        return jsonify(utils.ok({"blocks": height}))
    except Exception as exc:
        log.exception("GET /info")
        return jsonify(utils.err(500, str(exc))), 500


@bp.route("/balance/<string:address>", methods=["GET"])
@cross_origin()
def get_balance(address: str):
    try:
        validate_address(address)
        scripthash = address_to_scripthash(address)
    except ValueError as exc:
        return jsonify(utils.err(400, str(exc))), 400

    try:
        data  = _pool.call("blockchain.scripthash.get_balance", scripthash)
        total = data["confirmed"] + data["unconfirmed"]
        return jsonify(utils.ok({
            "balance":     total,
            "confirmed":   data["confirmed"],
            "unconfirmed": data["unconfirmed"],
        }))
    except Exception as exc:
        log.exception("GET /balance/%s", address)
        return jsonify(utils.err(500, str(exc))), 500


@bp.route("/unspent/<string:address>", methods=["GET"])
@cross_origin()
def get_unspent(address: str):
    try:
        validate_address(address)
    except ValueError as exc:
        return jsonify(utils.err(400, str(exc))), 400

    try:
        raw_amount = request.args.get("amount", "0")
        if not re.match(r'^\d{1,15}$', raw_amount):
            return jsonify(utils.err(400, "amount must be a non-negative integer")), 400
        min_value = int(raw_amount)
    except (ValueError, TypeError):
        min_value = 0

    confirmed_only = request.args.get("confirmed", "false").lower() == "true"

    try:
        scripthash = address_to_scripthash(address)
        script_hex = address_to_scriptpubkey(address).hex()
    except ValueError as exc:
        return jsonify(utils.err(400, str(exc))), 400

    try:
        utxos = _pool.call("blockchain.scripthash.listunspent", scripthash)
    except Exception as exc:
        log.exception("GET /unspent/%s", address)
        return jsonify(utils.err(500, str(exc))), 500

    # Filter before enriching  -  don't call is_coinbase_tx for discarded UTXOs
    filtered = [
        u for u in utxos
        if not (confirmed_only and u["height"] == 0)
        and not (min_value > 0 and u["value"] < min_value)
    ]

    def enrich_utxo(u):
        return {
            "txid":     u["tx_hash"],
            "index":    u["tx_pos"],
            "value":    u["value"],
            "height":   u["height"],
            "script":   script_hex,
            "coinbase": is_coinbase_tx(u["tx_hash"]),
        }

    if filtered:
        gpool  = gevent.pool.Pool(min(len(filtered), 20))
        result = list(gpool.imap(enrich_utxo, filtered))
    else:
        result = []
    return jsonify(utils.ok(result))


@bp.route("/fee", methods=["GET"])
@cross_origin()
def get_fee():
    return jsonify(utils.ok({"feerate": _FIXED_FEE_SATOSHIS}))


@bp.route("/tx/<string:txid>", methods=["GET"])
@cross_origin()
def get_tx(txid: str):
    """
    Return verbose transaction from ElectrumX.
    vout items get an extra field value_sat (int satoshis) to avoid float math.
    """
    try:
        validate_txid(txid)
    except ValueError as exc:
        return jsonify(utils.err(400, str(exc))), 400

    try:
        raw = _pool.call("blockchain.transaction.get", txid, True)
    except RuntimeError as exc:
        return jsonify(utils.err(400, str(exc))), 400
    except Exception as exc:
        log.exception("GET /tx/%s", txid)
        return jsonify(utils.err(500, str(exc))), 500

    for out in raw.get("vout", []):
        out["value_sat"] = int(round(out.get("value", 0) * 1e8))

    return jsonify(utils.ok(raw))


@bp.route("/history/<string:address>", methods=["GET"])
@cross_origin()
def get_history(address: str):
    """
    Return last N transactions for address, annotated with direction and amount.

    Algorithm (same as Electrum wallet  -  works for Legacy, SegWit, Taproot,
    multi-send, consolidation, send-to-self):

      1. blockchain.scripthash.get_history  -> list of {tx_hash, height}
      2. blockchain.transaction.get(txid, verbose=True) for each recent TX
      3. For every non-coinbase vin, fetch the previous TX to read the prevout.
      4. Direction:
           mine_in  = sum of value of inputs  whose prevout address == our address
           mine_out = sum of value of outputs whose address         == our address

           mine_in > 0, mine_out >= mine_in     -> 'self'
           mine_in > 0, has external out,
                        mine_out < mine_in       -> 'out'  (net = mine_in - mine_out)
           mine_in == 0, mine_out > 0            -> 'in'
           otherwise                             -> 'unknown'

    Query params:
      limit  -  max entries returned (default 10, max 50)
    """
    try:
        limit = min(int(request.args.get("limit", 10)), 50)
    except (ValueError, TypeError):
        limit = 10

    try:
        validate_address(address)
        scripthash = address_to_scripthash(address)
    except ValueError as exc:
        return jsonify(utils.err(400, str(exc))), 400

    try:
        with _history_cache_lock:
            raw_history = _history_cache.get(scripthash)

        if raw_history is None:
            raw_history = _pool.call("blockchain.scripthash.get_history", scripthash)
            with _history_cache_lock:
                if scripthash not in _history_cache:
                    if len(_history_cache) >= MAX_HISTORY_CACHE_SIZE:
                        evict = MAX_HISTORY_CACHE_SIZE // 2
                        for k in list(_history_cache.keys())[:evict]:
                            del _history_cache[k]
                        log.info("History cache evicted %d entries", evict)
                    _history_cache[scripthash] = raw_history

        history = []
        seen: set[str] = set()
        for h in raw_history:
            height = h["height"]
            if height < -1:
                continue          # undefined by ElectrumX protocol
            if height == -1:
                h = dict(h, height=0)  # mempool TX with unconfirmed inputs -> normalise
            # Deduplicate by txid  -  during a reorg ElectrumX can return the same
            # txid twice. Keep the first occurrence (confirmed takes priority).
            if h["tx_hash"] not in seen:
                seen.add(h["tx_hash"])
                history.append(h)

        # Reorg eviction: if a TX we cached as confirmed is back at height=0,
        # evict it so fetch_verbose_tx returns a fresh copy from the node.
        with _tx_cache_lock:
            for h in history:
                if h["height"] == 0 and h["tx_hash"] in _tx_cache:
                    log.debug("Evicting stale tx cache after reorg: %s", h["tx_hash"][:16])
                    del _tx_cache[h["tx_hash"]]

    except Exception as exc:
        log.exception("GET /history/%s  -  history fetch", address)
        return jsonify(utils.err(500, str(exc))), 500

    recent = history[-limit:][::-1]  # most-recent N, newest first
    if not recent:
        return jsonify(utils.ok([]))

    try:
        main_raw = fetch_verbose_tx_batch([item["tx_hash"] for item in recent])
        main_txs = [(item, main_raw.get(item["tx_hash"])) for item in recent]

        prevout_needed: set[str] = set()
        for _item, tx in main_txs:
            if tx is None:
                continue
            for vin in tx.get("vin", []):
                if "coinbase" not in vin and "txid" in vin:
                    prevout_needed.add(vin["txid"])

        prevout_map = fetch_verbose_tx_batch(list(prevout_needed)) if prevout_needed else {}

        results = []
        for item, tx in main_txs:
            if tx is None:
                results.append({
                    "txid": item["tx_hash"], "height": item["height"],
                    "timestamp": None, "direction": "unknown",
                    "amount": None, "mine_in": 0, "mine_out": 0,
                })
                continue

            timestamp = tx.get("blocktime") or tx.get("time") or None
            mine_in   = 0
            mine_out  = 0

            for vin in tx.get("vin", []):
                if "coinbase" in vin:
                    continue
                ptxid = vin.get("txid")
                pvout = vin.get("vout")
                if ptxid is None or pvout is None:
                    continue
                ptx = prevout_map.get(ptxid)
                if ptx is None:
                    continue
                pvouts = ptx.get("vout", [])
                if pvout < 0 or pvout >= len(pvouts):
                    continue
                if extract_vout_address(pvouts[pvout]) == address:
                    mine_in += vout_to_satoshis(pvouts[pvout])

            has_external_out = False
            for vout in tx.get("vout", []):
                addr = extract_vout_address(vout)
                if addr is None:
                    continue  # OP_RETURN / undecodable
                if addr == address:
                    mine_out += vout_to_satoshis(vout)
                else:
                    has_external_out = True

            if mine_in > 0:
                net = mine_in - mine_out
                if not has_external_out or mine_out >= mine_in:
                    direction = "self"
                    amount    = mine_out
                else:
                    direction = "out"
                    amount    = max(net, 0)
            elif mine_out > 0:
                direction = "in"
                amount    = mine_out
            else:
                direction = "unknown"
                amount    = 0

            results.append({
                "txid":      item["tx_hash"],
                "height":    item["height"],
                "timestamp": timestamp,
                "direction": direction,
                "amount":    amount,
                "mine_in":   mine_in,
                "mine_out":  mine_out,
            })

        return jsonify(utils.ok(results))

    except Exception as exc:
        log.exception("GET /history/%s", address)
        return jsonify(utils.err(500, str(exc))), 500


@bp.route("/rawtx/<string:txid>", methods=["GET"])
@cross_origin()
def get_raw_tx(txid: str):
    """
    Return raw transaction hex string.
    Required by the dpc wallet for signing legacy P2PKH inputs (nonWitnessUtxo).
    """
    try:
        validate_txid(txid)
    except ValueError as exc:
        return jsonify(utils.err(400, str(exc))), 400

    try:
        raw_hex = _pool.call("blockchain.transaction.get", txid, False)
        return jsonify(utils.ok(raw_hex))
    except RuntimeError as exc:
        return jsonify(utils.err(400, str(exc))), 400
    except Exception as exc:
        log.exception("GET /rawtx/%s", txid)
        return jsonify(utils.err(500, str(exc))), 500


@bp.route("/broadcast", methods=["POST"])
@cross_origin()
def broadcast():
    if request.content_length and request.content_length > _BROADCAST_MAX_BYTES:
        return jsonify(utils.err(400, "Request body too large (max 100 KB)")), 400

    raw_tx = request.values.get("raw") or request.get_data(as_text=True).strip()

    if not raw_tx:
        return jsonify(utils.err(400, "Missing raw transaction hex")), 400

    if len(raw_tx) > _BROADCAST_MAX_BYTES:
        return jsonify(utils.err(400, "Transaction hex too large (max 100 KB)")), 400

    if len(raw_tx) % 2 != 0 or not _RE_HEX.match(raw_tx):
        return jsonify(utils.err(400, "raw must be a valid hex string")), 400

    try:
        txid = _pool.call("blockchain.transaction.broadcast", raw_tx)
        return jsonify(utils.ok(txid))
    except RuntimeError as exc:
        msg = str(exc)
        log.warning("POST /broadcast rejected: %s", msg)
        return jsonify(utils.err(400, msg)), 400
    except Exception as exc:
        log.exception("POST /broadcast")
        return jsonify(utils.err(500, str(exc))), 500


# ---------------------------------------------------------------------------
# WebSocket  -  socket.io events
# ---------------------------------------------------------------------------

@socketio.on("connect")
def ws_on_connect():
    log.debug("WS connected: %s", request.sid)


@socketio.on("disconnect")
def ws_on_disconnect():
    with _sid_lock:
        _sid_last_sub.pop(request.sid, None)
        old_sh = _sid_rooms.pop(request.sid, None)
        if old_sh:
            count = _scripthash_refcount.get(old_sh, 1) - 1
            if count <= 0:
                _scripthash_refcount.pop(old_sh, None)
                _scripthash_scripts.pop(old_sh, None)
            else:
                _scripthash_refcount[old_sh] = count


@socketio.on("subscribe")
def ws_on_subscribe(data):
    if not isinstance(data, dict):
        return

    now = time.monotonic()
    with _sid_lock:
        if now - _sid_last_sub.get(request.sid, 0) < _SUB_MIN_INTERVAL:
            socketio.emit("error", {"message": "rate limited"}, to=request.sid)
            return
        _sid_last_sub[request.sid] = now

    address = str(data.get("address", "")).strip()
    try:
        validate_address(address)
        scripthash = address_to_scripthash(address)
        script_hex = address_to_scriptpubkey(address).hex()
    except ValueError as exc:
        socketio.emit("error", {"message": str(exc)}, to=request.sid)
        return

    with _sid_lock:
        old_room = _sid_rooms.get(request.sid)
        _sid_rooms[request.sid] = scripthash
        _scripthash_scripts[scripthash] = script_hex

        _scripthash_refcount[scripthash] = _scripthash_refcount.get(scripthash, 0) + 1

        if old_room and old_room != scripthash:
            count = _scripthash_refcount.get(old_room, 1) - 1
            if count <= 0:
                _scripthash_refcount.pop(old_room, None)
                _scripthash_scripts.pop(old_room, None)
            else:
                _scripthash_refcount[old_room] = count

    if old_room and old_room != scripthash:
        leave_room(old_room)

    join_room(scripthash)
    _subscriber.subscribe_scripthash(scripthash)
    socketio.emit("subscribed", {"address": address}, to=request.sid)

    sid = request.sid  # capture before greenlet  -  request context is gone inside

    def send_initial():
        try:
            bal       = _pool.call("blockchain.scripthash.get_balance", scripthash)
            raw_utxos = _pool.call("blockchain.scripthash.listunspent", scripthash)
            height    = get_tip_height()
            sx        = _scripthash_scripts.get(scripthash, "")

            def enrich_utxo(u):
                return {
                    "txid":     u["tx_hash"],
                    "index":    u["tx_pos"],
                    "value":    u["value"],
                    "height":   u["height"],
                    "coinbase": is_coinbase_tx(u["tx_hash"]),
                    "script":   sx,
                }

            if raw_utxos:
                gpool    = gevent.pool.Pool(min(len(raw_utxos), 20))
                enriched = list(gpool.imap(enrich_utxo, raw_utxos))
            else:
                enriched = []

            incoming_mempool = sum(u["value"] for u in raw_utxos if u["height"] == 0)
            pending_out      = max(0, incoming_mempool - bal["unconfirmed"])

            payload = {
                "balance":     bal["confirmed"] + bal["unconfirmed"],
                "confirmed":   bal["confirmed"],
                "unconfirmed": bal["unconfirmed"],
                "pending_out": pending_out,
                "utxos":       enriched,
                "height":      height,
            }
            socketio.emit("balance_changed", payload, to=sid)
        except Exception as e:
            log.warning("Initial push failed for %s: %s", scripthash[:12], e)

    gevent.spawn(send_initial)


# ---------------------------------------------------------------------------
# Subscriber callbacks
# ---------------------------------------------------------------------------

def on_new_block(height: int) -> None:
    _tip_cache["height"]  = height
    _tip_cache["expires"] = time.monotonic() + _TIP_TTL
    log.info("New block: height=%d  -  pushing to all WS clients", height)
    socketio.emit("block", {"height": height})


def on_scripthash_change(scripthash: str) -> None:
    log.debug("Balance changed: scripthash=%s...", scripthash[:12])

    with _history_cache_lock:
        _history_cache.pop(scripthash, None)

    def fetch_and_push():
        with _sid_lock:
            anyone = _scripthash_refcount.get(scripthash, 0) > 0
        if not anyone:
            return

        try:
            bal       = _pool.call("blockchain.scripthash.get_balance", scripthash)
            raw_utxos = _pool.call("blockchain.scripthash.listunspent", scripthash)
            # on_new_block always fires before on_scripthash_change for the same block,
            # so the tip cache is already fresh here.
            height    = _tip_cache.get("height", 0)
            sx        = _scripthash_scripts.get(scripthash, "")

            def enrich_utxo(u):
                return {
                    "txid":     u["tx_hash"],
                    "index":    u["tx_pos"],
                    "value":    u["value"],
                    "height":   u["height"],
                    "coinbase": is_coinbase_tx(u["tx_hash"]),
                    "script":   sx,
                }

            if raw_utxos:
                gpool    = gevent.pool.Pool(min(len(raw_utxos), 20))
                enriched = list(gpool.imap(enrich_utxo, raw_utxos))
            else:
                enriched = []

            incoming_mempool = sum(u["value"] for u in raw_utxos if u["height"] == 0)
            pending_out      = max(0, incoming_mempool - bal["unconfirmed"])

            payload = {
                "balance":     bal["confirmed"] + bal["unconfirmed"],
                "confirmed":   bal["confirmed"],
                "unconfirmed": bal["unconfirmed"],
                "pending_out": pending_out,
                "utxos":       enriched,
                "height":      height,
            }
            socketio.emit("balance_changed", payload, to=scripthash)

        except Exception as e:
            log.warning("Failed to push balance for scripthash %s: %s", scripthash[:12], e)

    gevent.spawn(fetch_and_push)


def start_subscriber() -> None:
    _subscriber.on_new_block         = on_new_block
    _subscriber.on_scripthash_change = on_scripthash_change
    _subscriber.start()
    log.info("ElectrumX subscriber started")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def init(app):
    app.register_blueprint(bp, url_prefix="/")
    start_subscriber()
