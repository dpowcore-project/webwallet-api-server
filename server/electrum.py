"""
ElectrumX JSON-RPC client over TLS.

- ElectrumClient      - single thread-safe connection with auto-reconnect
- ElectrumPool        - fixed pool of N clients, round-robin via queue
- ElectrumSubscriber  - dedicated connection for server-push notifications
"""

import json
import logging
import queue
import socket
import ssl
import threading
import time

log = logging.getLogger(__name__)

CLIENT_NAME    = "DpowcoinWallet"
CLIENT_VERSION = "1.0"
PROTOCOL_MIN   = "1.4"
PROTOCOL_MAX   = "1.4"

# ElectrumX closes idle connections after ~240 s; ping every 3 min to stay alive.
PING_INTERVAL = 180


class ElectrumClient:
    """Thread-safe ElectrumX connection. Reconnects once on socket error."""

    def __init__(self, host, port, timeout=15, verify_ssl=True):
        self.host       = host
        self.port       = port
        self.timeout    = timeout
        self.verify_ssl = verify_ssl
        self._sock      = None
        self._buf       = b""
        self._req_id    = 0
        self._lock      = threading.Lock()

    def make_ssl_context(self):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
        return ctx

    def open_connection(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self._buf  = b""
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(self.timeout)
        raw.connect((self.host, self.port))
        self._sock = self.make_ssl_context().wrap_socket(raw, server_hostname=self.host)
        log.info("Connected to %s:%s", self.host, self.port)
        self.send_rpc("server.version", [f"{CLIENT_NAME} {CLIENT_VERSION}", [PROTOCOL_MIN, PROTOCOL_MAX]])

    def send_rpc(self, method, params):
        self._req_id += 1
        req_id = self._req_id
        self._sock.sendall((json.dumps({"id": req_id, "method": method, "params": params}) + "\n").encode())
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                resp = json.loads(line)
                if resp.get("id") != req_id:
                    continue  # server-push notification, not our response
                if resp.get("error"):
                    raise RuntimeError(f"ElectrumX error: {resp['error']}")
                return resp["result"]
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("Server closed the connection")
            self._buf += chunk

    def send_rpc_batch(self, requests):
        """
        Pipeline N requests over one connection in a single write.

        requests : list of (method, params_list)
        returns  : list of results aligned to input order.
                   Per-item ElectrumX errors produce None; transport errors raise.
        """
        if not requests:
            return []

        ids   = []
        lines = []
        for method, params in requests:
            self._req_id += 1
            req_id = self._req_id
            ids.append(req_id)
            lines.append(json.dumps({"id": req_id, "method": method, "params": params}))

        self._sock.sendall(("\n".join(lines) + "\n").encode())

        pending = set(ids)
        results = {}

        while pending:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                resp = json.loads(line)
                rid  = resp.get("id")
                if rid not in pending:
                    continue
                pending.discard(rid)
                results[rid] = None if resp.get("error") else resp["result"]
            if pending:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Server closed the connection")
                self._buf += chunk

        return [results[req_id] for req_id in ids]

    def call(self, method, *params):
        with self._lock:
            try:
                if not self._sock:
                    self.open_connection()
                return self.send_rpc(method, list(params))
            except (OSError, ConnectionError, ssl.SSLError) as exc:
                log.warning("Socket error on %s (%s)  - reconnecting", method, exc)
                self.open_connection()
                return self.send_rpc(method, list(params))

    def call_batch(self, requests):
        """
        Send multiple RPC calls in one round-trip (pipelining).

        requests : list of (method, params_list)
        returns  : list of results aligned to input order (None on per-item error).
        """
        with self._lock:
            try:
                if not self._sock:
                    self.open_connection()
                return self.send_rpc_batch(requests)
            except (OSError, ConnectionError, ssl.SSLError) as exc:
                log.warning("Socket error on batch (%s)  - reconnecting", exc)
                self.open_connection()
                return self.send_rpc_batch(requests)

    def close(self):
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
            self._buf = b""


class ElectrumPool:
    """Round-robin pool of ElectrumClient connections."""

    def __init__(self, host, port, timeout, verify_ssl, size=8):
        self._queue = queue.Queue()
        for _ in range(size):
            self._queue.put(ElectrumClient(host, port, timeout, verify_ssl))

    def call(self, method, *params):
        client = self._queue.get()
        try:
            return client.call(method, *params)
        finally:
            self._queue.put(client)

    def call_batch(self, requests):
        """
        Acquire one connection and send all requests as a pipelined batch.

        requests : list of (method, params_list)
        returns  : list of results aligned to input order.
        """
        client = self._queue.get()
        try:
            return client.call_batch(requests)
        finally:
            self._queue.put(client)

    def close(self):
        while not self._queue.empty():
            try:
                self._queue.get_nowait().close()
            except Exception:
                pass


class ElectrumSubscriber:
    """
    Persistent connection for server-push notifications.

    Keepalive: recv runs with settimeout(PING_INTERVAL); on socket.timeout
    a server.ping is sent so ElectrumX doesn't close the idle connection.
    """

    def __init__(self, host, port, timeout, verify_ssl):
        self.host       = host
        self.port       = port
        self.timeout    = timeout
        self.verify_ssl = verify_ssl

        self._sock      = None
        self._buf       = b""
        self._req_id    = 0
        self._send_lock = threading.Lock()

        self._subscribed = set()
        self._sub_lock   = threading.Lock()

        self._running = False

        self.on_new_block         = None  # callable(height: int)
        self.on_scripthash_change = None  # callable(scripthash: str)

    def start(self):
        self._running = True
        t = threading.Thread(target=self.run_loop, daemon=True, name="electrum-subscriber")
        t.start()

    def subscribe_scripthash(self, scripthash):
        """Idempotent, thread-safe."""
        with self._sub_lock:
            if scripthash in self._subscribed:
                return
            self._subscribed.add(scripthash)
        if self._sock:
            self.send_message("blockchain.scripthash.subscribe", [scripthash])

    def run_loop(self):
        while self._running:
            try:
                self.open_connection()
                self.read_loop()
            except Exception as exc:
                log.warning("Subscriber error: %s  - reconnecting in 5 s", exc)
                self.close_connection()
                time.sleep(5)

    def open_connection(self):
        self.close_connection()
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(self.timeout)
        raw.connect((self.host, self.port))
        self._sock = ctx.wrap_socket(raw, server_hostname=self.host)
        self._sock.settimeout(PING_INTERVAL)
        self._buf = b""
        log.info("Subscriber connected to %s:%s", self.host, self.port)

        self.send_message("server.version",              [f"{CLIENT_NAME} {CLIENT_VERSION}", [PROTOCOL_MIN, PROTOCOL_MAX]])
        self.send_message("blockchain.headers.subscribe", [])

        with self._sub_lock:
            hashes = list(self._subscribed)
        for sh in hashes:
            self.send_message("blockchain.scripthash.subscribe", [sh])

    def send_message(self, method, params):
        with self._send_lock:
            self._req_id += 1
            payload = json.dumps({"id": self._req_id, "method": method, "params": params}) + "\n"
            if self._sock:
                self._sock.sendall(payload.encode())

    def read_loop(self):
        while True:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                log.debug("Subscriber idle  - sending server.ping")
                self.send_message("server.ping", [])
                continue

            if not chunk:
                raise ConnectionError("Server closed connection")

            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    self.dispatch_message(json.loads(line))
                except Exception:
                    pass

    def dispatch_message(self, msg):
        method = msg.get("method")
        if not method:
            return  # response to our own request (server.version / server.ping / subscribe)

        params = msg.get("params") or []

        if method == "blockchain.headers.subscribe":
            if params and isinstance(params[0], dict) and self.on_new_block:
                height = params[0].get("height")
                if height is not None:
                    try:
                        self.on_new_block(int(height))
                    except Exception:
                        pass

        elif method == "blockchain.scripthash.subscribe":
            if params and self.on_scripthash_change:
                try:
                    self.on_scripthash_change(str(params[0]))
                except Exception:
                    pass

    def close_connection(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._buf = b""
