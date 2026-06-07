#!/usr/bin/env python3
"""
netboot.py — Combined DHCP + TFTP server for PXE netbooting.

Designed for local development / Android bringup workflows.
Requires root/Administrator (ports 67 and 69).

Usage:
    python netboot.py [--config netboot.toml] [overrides...]
    python netboot.py --init          # write a starter netboot.toml and exit
"""

from __future__ import annotations

import sys
sys.stdout.reconfigure(line_buffering=True)
import argparse
import csv
import ipaddress
import logging
import signal
import socket
import struct
import threading
import time
import tomllib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(component)s] %(levelname)s %(message)s"


def _make_logger(component: str) -> logging.LoggerAdapter:
    logger = logging.getLogger(component)
    return logging.LoggerAdapter(logger, {"component": component})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "dhcp": {
        "interface": "",
        "server_ip": "192.168.1.1",
        "subnet_mask": "255.255.255.0",
        "router": "192.168.1.1",
        "dns": ["8.8.8.8"],
        "pool_start": "192.168.1.100",
        "pool_end": "192.168.1.200",
        "lease_time": 86400,
        "lease_file": "leases.csv",
    },
    "tftp": {
        "interface": "0.0.0.0",
        "port": 69,
        "root": "./tftp-root",
    },
    "pxe": {
        "enabled": False,
        "boot_file": "pxelinux.0",
    },
}

STARTER_TOML = """\
# netboot.toml — configuration for netboot.py
# Run `python netboot.py --init` to regenerate this file.

[dhcp]
# Network interface to bind (empty string = all interfaces / INADDR_ANY).
interface   = ""
# IP address of this machine on the PXE network.
server_ip   = "192.168.1.1"
subnet_mask = "255.255.255.0"
router      = "192.168.1.1"
dns         = ["8.8.8.8"]
# Assignable IP pool for PXE clients.
pool_start  = "192.168.1.100"
pool_end    = "192.168.1.200"
# Lease duration in seconds (default 24 h).
lease_time  = 86400
# Path to the CSV file that persists MAC→IP bindings.
lease_file  = "leases.csv"

[tftp]
# Interface for the TFTP server (0.0.0.0 = all).
interface = "0.0.0.0"
port      = 69
# Directory from which files are served.
root      = "./tftp-root"

[pxe]
# Filename sent in DHCP option 67 (boot file name).
# Common values: pxelinux.0, grubx64.efi, ipxe.efi
boot_file = "pxelinux.0"
"""


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: Path | None, cli_overrides: dict) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    # Deep-copy nested dicts
    cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in cfg.items()}
    for section in cfg:
        if isinstance(cfg[section], dict):
            cfg[section] = dict(cfg[section])

    if config_path and config_path.exists():
        with open(config_path, "rb") as fh:
            file_cfg = tomllib.load(fh)
        cfg = _deep_merge(cfg, file_cfg)

    # Apply CLI overrides (only keys that were explicitly set)
    for dotted_key, value in cli_overrides.items():
        if value is None:
            continue
        section, _, key = dotted_key.partition(".")
        if section in cfg and isinstance(cfg[section], dict):
            cfg[section][key] = value

    return cfg


# ---------------------------------------------------------------------------
# Lease store
# ---------------------------------------------------------------------------

CSV_FIELDS = ["mac", "ip", "hostname", "lease_start", "lease_expiry"]


@dataclass
class Lease:
    mac: str
    ip: str
    hostname: str
    lease_start: float
    lease_expiry: float

    def is_expired(self) -> bool:
        return time.time() > self.lease_expiry


def _read_leases(path: Path) -> dict[str, Lease]:
    leases: dict[str, Lease] = {}
    if not path.exists():
        return leases
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                lease = Lease(
                    mac=row["mac"].lower(),
                    ip=row["ip"],
                    hostname=row.get("hostname", ""),
                    lease_start=float(row["lease_start"]),
                    lease_expiry=float(row["lease_expiry"]),
                )
                leases[lease.mac] = lease
            except (KeyError, ValueError):
                continue
    return leases


def _write_leases(path: Path, leases: dict[str, Lease]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for lease in leases.values():
            writer.writerow(
                {
                    "mac": lease.mac,
                    "ip": lease.ip,
                    "hostname": lease.hostname,
                    "lease_start": lease.lease_start,
                    "lease_expiry": lease.lease_expiry,
                }
            )


def _assign_ip(
    mac: str,
    hostname: str,
    leases: dict[str, Lease],
    pool_start: str,
    pool_end: str,
    lease_time: int,
) -> Lease | None:
    """Return an existing valid lease or allocate a new IP from the pool."""
    mac = mac.lower()

    # Reuse existing non-expired lease for this MAC
    if mac in leases and not leases[mac].is_expired():
        lease = leases[mac]
        # Renew expiry
        lease.lease_expiry = time.time() + lease_time
        return lease

    # Collect IPs already in use by non-expired leases
    used_ips = {
        lease.ip
        for lease in leases.values()
        if not lease.is_expired() and lease.mac != mac
    }

    start = int(ipaddress.IPv4Address(pool_start))
    end = int(ipaddress.IPv4Address(pool_end))
    for ip_int in range(start, end + 1):
        candidate = str(ipaddress.IPv4Address(ip_int))
        if candidate not in used_ips:
            now = time.time()
            lease = Lease(
                mac=mac,
                ip=candidate,
                hostname=hostname,
                lease_start=now,
                lease_expiry=now + lease_time,
            )
            leases[mac] = lease
            return lease

    return None  # pool exhausted


# ---------------------------------------------------------------------------
# DHCP packet helpers
# ---------------------------------------------------------------------------

DHCP_MAGIC = b"\x63\x82\x53\x63"

DHCP_DISCOVER = 1
DHCP_OFFER = 2
DHCP_REQUEST = 3
DHCP_DECLINE = 4
DHCP_ACK = 5
DHCP_NAK = 6
DHCP_RELEASE = 7
DHCP_INFORM = 8

MSG_TYPE_NAMES = {
    DHCP_DISCOVER: "DISCOVER",
    DHCP_OFFER: "OFFER",
    DHCP_REQUEST: "REQUEST",
    DHCP_DECLINE: "DECLINE",
    DHCP_ACK: "ACK",
    DHCP_NAK: "NAK",
    DHCP_RELEASE: "RELEASE",
    DHCP_INFORM: "INFORM",
}


def _parse_dhcp(data: bytes) -> dict | None:
    """Parse a DHCP packet into a dict. Returns None on parse error."""
    if len(data) < 240:
        return None
    pkt: dict[str, Any] = {}
    pkt["op"] = data[0]
    pkt["htype"] = data[1]
    pkt["hlen"] = data[2]
    pkt["xid"] = struct.unpack("!I", data[4:8])[0]
    pkt["flags"] = struct.unpack("!H", data[10:12])[0]
    pkt["ciaddr"] = socket.inet_ntoa(data[12:16])
    pkt["yiaddr"] = socket.inet_ntoa(data[16:20])
    pkt["siaddr"] = socket.inet_ntoa(data[20:24])
    pkt["giaddr"] = socket.inet_ntoa(data[24:28])
    hlen = pkt["hlen"]
    pkt["chaddr"] = data[28 : 28 + hlen]
    pkt["mac"] = ":".join(f"{b:02x}" for b in pkt["chaddr"])
    # sname (64 bytes) and file (128 bytes) — skip
    pkt["sname"] = data[44:108].rstrip(b"\x00").decode("ascii", errors="replace")
    pkt["file"] = data[108:236].rstrip(b"\x00").decode("ascii", errors="replace")

    if data[236:240] != DHCP_MAGIC:
        return None

    # Parse options
    options: dict[int, bytes] = {}
    idx = 240
    while idx < len(data):
        opt = data[idx]
        if opt == 255:  # END
            break
        if opt == 0:  # PAD
            idx += 1
            continue
        if idx + 1 >= len(data):
            break
        length = data[idx + 1]
        value = data[idx + 2 : idx + 2 + length]
        options[opt] = value
        idx += 2 + length

    pkt["options"] = options
    pkt["msg_type"] = options.get(53, b"\x00")[0] if 53 in options else 0

    # Hostname (option 12)
    pkt["hostname"] = (
        options[12].decode("ascii", errors="replace") if 12 in options else ""
    )

    # Requested IP (option 50)
    pkt["requested_ip"] = (
        socket.inet_ntoa(options[50]) if 50 in options else ""
    )

    return pkt


def _build_dhcp_reply(
    msg_type: int,
    xid: int,
    chaddr: bytes,
    hlen: int,
    yiaddr: str,
    server_ip: str,
    subnet_mask: str,
    router: str,
    dns_list: list[str],
    lease_time: int,
    boot_file: str,
    giaddr: str = "0.0.0.0",
    pxe_enabled: bool = True,
) -> bytes:
    buf = BytesIO()

    # Fixed header
    buf.write(bytes([2, 1, hlen, 0]))  # op=BOOTREPLY, htype=Ethernet, hlen, hops
    buf.write(struct.pack("!I", xid))
    buf.write(b"\x00\x00")  # secs
    buf.write(b"\x80\x00")  # flags: broadcast
    buf.write(socket.inet_aton("0.0.0.0"))  # ciaddr
    buf.write(socket.inet_aton(yiaddr))  # yiaddr
    buf.write(socket.inet_aton(server_ip if pxe_enabled else "0.0.0.0"))  # siaddr (next-server)
    buf.write(socket.inet_aton(giaddr))  # giaddr
    padded_chaddr = chaddr + b"\x00" * (16 - len(chaddr))
    buf.write(padded_chaddr)  # chaddr (16 bytes)

    # sname: server hostname (64 bytes) — only when PXE active
    sname = (server_ip.encode()[:63] + b"\x00") if pxe_enabled else b"\x00"
    sname = sname.ljust(64, b"\x00")
    buf.write(sname)

    # file: boot filename (128 bytes) — only when PXE active
    bfile = (boot_file.encode()[:127] + b"\x00") if pxe_enabled else b"\x00"
    bfile = bfile.ljust(128, b"\x00")
    buf.write(bfile)

    # Magic cookie
    buf.write(DHCP_MAGIC)

    def opt(code: int, value: bytes) -> bytes:
        return bytes([code, len(value)]) + value

    # Option 53: DHCP message type
    buf.write(opt(53, bytes([msg_type])))
    # Option 54: server identifier
    buf.write(opt(54, socket.inet_aton(server_ip)))
    # Option 51: lease time
    buf.write(opt(51, struct.pack("!I", lease_time)))
    # Option 1: subnet mask
    buf.write(opt(1, socket.inet_aton(subnet_mask)))
    # Option 3: router
    buf.write(opt(3, socket.inet_aton(router)))
    # Option 6: DNS
    dns_bytes = b"".join(socket.inet_aton(d) for d in dns_list)
    buf.write(opt(6, dns_bytes))
    # Options 66/67 only when PXE is enabled
    if pxe_enabled:
        buf.write(opt(66, server_ip.encode()))
        buf.write(opt(67, boot_file.encode()))
    # END
    buf.write(b"\xff")

    return buf.getvalue()


def _build_nak(xid: int, chaddr: bytes, hlen: int, server_ip: str) -> bytes:
    buf = BytesIO()
    buf.write(bytes([2, 1, hlen, 0]))
    buf.write(struct.pack("!I", xid))
    buf.write(b"\x00\x00\x80\x00")
    buf.write(socket.inet_aton("0.0.0.0") * 4)
    padded_chaddr = chaddr + b"\x00" * (16 - len(chaddr))
    buf.write(padded_chaddr)
    buf.write(b"\x00" * 64)   # sname
    buf.write(b"\x00" * 128)  # file
    buf.write(DHCP_MAGIC)

    def opt(code: int, value: bytes) -> bytes:
        return bytes([code, len(value)]) + value

    buf.write(opt(53, bytes([DHCP_NAK])))
    buf.write(opt(54, socket.inet_aton(server_ip)))
    buf.write(b"\xff")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# DHCP Server
# ---------------------------------------------------------------------------

class DHCPServer:
    def __init__(self, cfg: dict, stop_event: threading.Event) -> None:
        self.cfg = cfg["dhcp"]
        self.pxe = cfg["pxe"]
        self.server_ip: str = self.cfg["server_ip"]
        self.subnet_mask: str = self.cfg["subnet_mask"]
        self.router: str = self.cfg["router"]
        self.dns: list[str] = self.cfg["dns"]
        self.pool_start: str = self.cfg["pool_start"]
        self.pool_end: str = self.cfg["pool_end"]
        self.lease_time: int = int(self.cfg["lease_time"])
        self.lease_file = Path(self.cfg["lease_file"])
        self.pxe_enabled: bool = True
        self.boot_file: str = self.pxe["boot_file"]
        self.stop_event = stop_event
        self.log = _make_logger("DHCP")
        self._lock = threading.Lock()

    def _send_reply(self, sock: socket.socket, reply: bytes, giaddr: str) -> None:
        if giaddr and giaddr != "0.0.0.0":
            sock.sendto(reply, (giaddr, 67))
        else:
            sock.sendto(reply, ("255.255.255.255", 68))

    def _handle_packet(self, sock: socket.socket, data: bytes, addr: tuple) -> None:
        pkt = _parse_dhcp(data)
        if pkt is None:
            return

        msg_type = pkt["msg_type"]
        mac = pkt["mac"]
        xid = pkt["xid"]
        hostname = pkt["hostname"] or mac
        type_name = MSG_TYPE_NAMES.get(msg_type, f"UNKNOWN({msg_type})")
        self.log.info(f"{type_name} from {mac} (hostname={hostname!r})")

        if msg_type == DHCP_DISCOVER:
            with self._lock:
                leases = _read_leases(self.lease_file)
                lease = _assign_ip(
                    mac, hostname, leases,
                    self.pool_start, self.pool_end, self.lease_time,
                )
                if lease is None:
                    self.log.warning(f"Pool exhausted, cannot offer IP to {mac}")
                    return
                _write_leases(self.lease_file, leases)

            reply = _build_dhcp_reply(
                DHCP_OFFER, xid, pkt["chaddr"], pkt["hlen"],
                lease.ip, self.server_ip, self.subnet_mask,
                self.router, self.dns, self.lease_time,
                self.boot_file, pkt["giaddr"],
                self.pxe_enabled,
            )
            self.log.info(f"OFFER {lease.ip} → {mac} (boot={self.boot_file})")
            self._send_reply(sock, reply, pkt["giaddr"])

        elif msg_type == DHCP_REQUEST:
            with self._lock:
                leases = _read_leases(self.lease_file)
                lease = _assign_ip(
                    mac, hostname, leases,
                    self.pool_start, self.pool_end, self.lease_time,
                )
                if lease is None:
                    self.log.warning(f"Pool exhausted, sending NAK to {mac}")
                    nak = _build_nak(xid, pkt["chaddr"], pkt["hlen"], self.server_ip)
                    self._send_reply(sock, nak, pkt["giaddr"])
                    return
                _write_leases(self.lease_file, leases)

            reply = _build_dhcp_reply(
                DHCP_ACK, xid, pkt["chaddr"], pkt["hlen"],
                lease.ip, self.server_ip, self.subnet_mask,
                self.router, self.dns, self.lease_time,
                self.boot_file, pkt["giaddr"],
                self.pxe_enabled,
            )
            self.log.info(f"ACK {lease.ip} → {mac} (boot={self.boot_file})")
            self._send_reply(sock, reply, pkt["giaddr"])

        elif msg_type == DHCP_RELEASE:
            with self._lock:
                leases = _read_leases(self.lease_file)
                if mac in leases:
                    released_ip = leases[mac].ip
                    del leases[mac]
                    _write_leases(self.lease_file, leases)
                    self.log.info(f"RELEASE {released_ip} from {mac}")

        elif msg_type == DHCP_INFORM:
            # ACK with no yiaddr, no lease time
            reply = _build_dhcp_reply(
                DHCP_ACK, xid, pkt["chaddr"], pkt["hlen"],
                "0.0.0.0", self.server_ip, self.subnet_mask,
                self.router, self.dns, 0,
                self.boot_file, pkt["giaddr"],
                self.pxe_enabled,
            )
            self._send_reply(sock, reply, pkt["giaddr"])

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        bind_ip = self.cfg.get("interface") or "0.0.0.0"
        sock.bind((bind_ip, 67))
        sock.settimeout(1.0)
        self.log.info(
            f"Listening on {bind_ip}:67 | pool {self.pool_start}-{self.pool_end} "
            + (f"| pxe=enabled boot={self.boot_file} next-server={self.server_ip}" if self.pxe_enabled else "| pxe=disabled")
        )
        try:
            while not self.stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                try:
                    self._handle_packet(sock, data, addr)
                except Exception as exc:
                    self.log.error(f"Error handling packet from {addr}: {exc}", exc_info=True)
        finally:
            sock.close()
            self.log.info("Stopped.")


# ---------------------------------------------------------------------------
# TFTP Server
# ---------------------------------------------------------------------------

TFTP_RRQ   = 1
TFTP_DATA  = 3
TFTP_ACK   = 4
TFTP_ERROR = 5

TFTP_ERRORS = {
    0: "Not defined",
    1: "File not found",
    2: "Access violation",
    3: "Disk full",
    4: "Illegal TFTP operation",
    5: "Unknown transfer ID",
}

TFTP_BLOCK_SIZE = 512
TFTP_TIMEOUT    = 3.0   # seconds per block ACK
TFTP_RETRIES    = 5


def _tftp_error_pkt(code: int, msg: str = "") -> bytes:
    msg_bytes = (msg or TFTP_ERRORS.get(code, "Error")).encode() + b"\x00"
    return struct.pack("!HH", TFTP_ERROR, code) + msg_bytes


def _safe_resolve(root: Path, requested: str) -> Path | None:
    """Resolve requested path under root; return None if traversal detected."""
    # Normalise separators, strip leading slashes
    clean = requested.replace("\\", "/").lstrip("/")
    resolved = (root / clean).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def _tftp_send_file(
    server_sock: socket.socket,
    client_addr: tuple,
    file_path: Path,
    log: logging.LoggerAdapter,
) -> None:
    """Handle a single TFTP RRQ transfer in the calling thread."""
    # Each transfer uses a new ephemeral socket
    xfer_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    xfer_sock.settimeout(TFTP_TIMEOUT)
    xfer_sock.bind(("", 0))  # OS picks ephemeral port
    local_port = xfer_sock.getsockname()[1]

    try:
        with open(file_path, "rb") as fh:
            data = fh.read()

        total_blocks = max(1, -(-len(data) // TFTP_BLOCK_SIZE))  # ceil div
        log_interval = max(1, min(1000, total_blocks // 10))  # progress every ~10%
        log.info(
            f"[{client_addr[0]}:{client_addr[1]}] RRQ {file_path.name}"
            f" {len(data)} bytes / {total_blocks} blocks -> local port {local_port}"
        )

        block_num = 0
        offset = 0
        while True:
            block_num += 1
            wire_block = block_num % 65536  # TFTP block# is 16-bit
            chunk = data[offset : offset + TFTP_BLOCK_SIZE]
            pkt = struct.pack("!HH", TFTP_DATA, wire_block) + chunk

            for attempt in range(1, TFTP_RETRIES + 1):
                xfer_sock.sendto(pkt, client_addr)
                try:
                    ack_data, ack_addr = xfer_sock.recvfrom(512)
                    if len(ack_data) >= 4:
                        opcode, ack_block = struct.unpack("!HH", ack_data[:4])
                        if opcode == TFTP_ACK and ack_block == wire_block:
                            if block_num % log_interval == 0:
                                pct = int(block_num / total_blocks * 100)
                                log.info(f"[{client_addr[0]}] {file_path.name} {pct}% ({block_num}/{total_blocks} blocks)")
                            break
                        if opcode == TFTP_ERROR:
                            err_msg = ack_data[4:].rstrip(b"\x00").decode("ascii", errors="replace")
                            log.warning(f"Client sent ERROR: {err_msg}")
                            return
                except socket.timeout:
                    log.debug(f"Timeout waiting for ACK block {block_num}, attempt {attempt}")
                    if attempt == TFTP_RETRIES:
                        log.warning(f"Transfer of {file_path.name} to {client_addr[0]} timed out")
                        return

            offset += TFTP_BLOCK_SIZE
            if len(chunk) < TFTP_BLOCK_SIZE:
                # Last block sent and ACKed
                log.info(f"[{client_addr[0]}:{client_addr[1]}] {file_path.name} complete ({len(data)} bytes, {total_blocks} blocks)")
                break
    except OSError as exc:
        err_pkt = _tftp_error_pkt(0, str(exc))
        xfer_sock.sendto(err_pkt, client_addr)
        log.error(f"IO error sending {file_path}: {exc}")
    finally:
        xfer_sock.close()


class TFTPServer:
    def __init__(self, cfg: dict, stop_event: threading.Event) -> None:
        self.cfg = cfg["tftp"]
        self.root = Path(self.cfg["root"]).resolve()
        self.interface: str = self.cfg.get("interface", "0.0.0.0")
        self.port: int = int(self.cfg.get("port", 69))
        self.stop_event = stop_event
        self.log = _make_logger("TFTP")

    def _handle_rrq(self, data: bytes, client_addr: tuple, sock: socket.socket) -> None:
        # RRQ format: opcode(2) | filename\0 | mode\0
        payload = data[2:]
        parts = payload.split(b"\x00")
        if len(parts) < 2:
            sock.sendto(_tftp_error_pkt(4, "Malformed RRQ"), client_addr)
            return

        filename = parts[0].decode("ascii", errors="replace")
        mode = parts[1].decode("ascii", errors="replace").lower()

        if mode != "octet":
            sock.sendto(_tftp_error_pkt(4, f"Unsupported mode: {mode}"), client_addr)
            self.log.warning(f"Rejected non-octet RRQ from {client_addr[0]}: mode={mode}")
            return

        file_path = _safe_resolve(self.root, filename)
        if file_path is None:
            sock.sendto(_tftp_error_pkt(2, "Access violation"), client_addr)
            self.log.warning(f"Path traversal attempt from {client_addr[0]}: {filename!r}")
            return

        if not file_path.exists() or not file_path.is_file():
            sock.sendto(_tftp_error_pkt(1, "File not found"), client_addr)
            self.log.warning(f"File not found: {filename!r} (requested by {client_addr[0]})")
            return

        self.log.info(f"RRQ {filename!r} from {client_addr[0]}")
        thread = threading.Thread(
            target=_tftp_send_file,
            args=(sock, client_addr, file_path, self.log),
            daemon=True,
        )
        thread.start()

    def run(self) -> None:
        if not self.root.exists():
            self.log.warning(f"TFTP root {self.root} does not exist — creating it.")
            self.root.mkdir(parents=True, exist_ok=True)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.interface, self.port))
        sock.settimeout(1.0)
        self.log.info(f"Listening on {self.interface}:{self.port} | root={self.root}")

        try:
            while not self.stop_event.is_set():
                try:
                    data, client_addr = sock.recvfrom(516)
                except socket.timeout:
                    continue
                try:
                    if len(data) < 2:
                        continue
                    opcode = struct.unpack("!H", data[:2])[0]
                    self.log.debug(f"Packet from {client_addr[0]}:{client_addr[1]} opcode={opcode} len={len(data)}")
                    if opcode == TFTP_RRQ:
                        self._handle_rrq(data, client_addr, sock)
                    else:
                        self.log.warning(f"Unsupported opcode {opcode} from {client_addr[0]}:{client_addr[1]}")
                        sock.sendto(_tftp_error_pkt(4, "Only RRQ supported"), client_addr)
                except Exception as exc:
                    self.log.error(f"Error handling request from {client_addr}: {exc}", exc_info=True)
        finally:
            sock.close()
            self.log.info("Stopped.")


# ---------------------------------------------------------------------------
# Serial monitor
# ---------------------------------------------------------------------------



class SerialMonitor:
    """Reads a serial port and prints output to stdout.
    """

    def __init__(self, port: str, baud: int, stop_event: threading.Event) -> None:
        self.port = port
        self.baud = baud
        self.stop_event = stop_event
        self.log = _make_logger("SERIAL")

    def run(self) -> None:
        try:
            import serial as _serial
        except ImportError:
            self.log.error("pyserial is not installed — run: pip install pyserial")
            return

        while not self.stop_event.is_set():
            self.log.info(f"Opening serial port {self.port} at {self.baud} baud")
            try:
                ser = _serial.Serial(self.port, self.baud, timeout=0.1)
            except _serial.SerialException as exc:
                self.log.warning(f"Cannot open {self.port}: {exc} — retrying in 2s")
                self.stop_event.wait(timeout=2)
                continue

            # stdin -> serial thread
            def _stdin_to_serial():
                try:
                    for line in sys.stdin:
                        ser.write(line.encode())
                except Exception:
                    pass

            stdin_thread = threading.Thread(target=_stdin_to_serial, daemon=True)
            stdin_thread.start()

            # serial -> stdout (main loop)
            try:
                while not self.stop_event.is_set():
                    try:
                        chunk = ser.read(ser.in_waiting or 1).decode("ascii", errors="replace")
                    except _serial.SerialException:
                        self.log.warning(f"{self.port} disconnected — reconnecting...")
                        break
                    if chunk:
                        print(chunk, end="")
                    else:
                        time.sleep(0.01)
            finally:
                try:
                    ser.close()
                except Exception:
                    pass

        self.log.info("Serial port closed.")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DHCP + TFTP server for PXE netbooting.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default="netboot.toml", metavar="FILE",
                        help="Path to TOML config file")
    parser.add_argument("--init", action="store_true",
                        help="Write a starter netboot.toml and exit")
    parser.add_argument("--log-level", default="DEBUG",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity")

    # DHCP overrides
    dg = parser.add_argument_group("DHCP overrides")
    dg.add_argument("--server-ip",    metavar="IP",   help="Server IP (DHCP option 54 / TFTP next-server)")
    dg.add_argument("--pool-start",   metavar="IP",   help="First IP in the assignable pool")
    dg.add_argument("--pool-end",     metavar="IP",   help="Last IP in the assignable pool")
    dg.add_argument("--lease-file",   metavar="PATH", help="Path to the CSV lease file")
    dg.add_argument("--lease-time",   metavar="SEC",  type=int, help="Lease duration in seconds")
    dg.add_argument("--dhcp-iface",   metavar="IP",   help="IP/interface to bind DHCP socket")

    # TFTP overrides
    tg = parser.add_argument_group("TFTP overrides")
    tg.add_argument("--tftp-root",    metavar="DIR",  help="Directory to serve files from")
    tg.add_argument("--tftp-port",    metavar="PORT", type=int, help="TFTP UDP port")
    tg.add_argument("--tftp-iface",   metavar="IP",   help="IP/interface to bind TFTP socket")

    # PXE overrides
    pg = parser.add_argument_group("PXE overrides")
    pg.add_argument("--boot-file",    metavar="FILE", help="Boot filename (DHCP option 67)")


    # Serial
    sg = parser.add_argument_group("Serial")
    sg.add_argument("--serial",        metavar="PORT", help="Serial port to monitor (e.g. COM3 or /dev/ttyUSB0)")
    sg.add_argument("--serial-baud",   metavar="BAUD", type=int, default=115200, help="Serial baud rate")
    sg.add_argument("--serial-only", action="store_true",
                    help="Only run the serial monitor, skip DHCP and TFTP servers")

    return parser

def _cli_overrides(args: argparse.Namespace) -> dict:
    mapping = {
        "dhcp.server_ip":  args.server_ip,
        "dhcp.pool_start": args.pool_start,
        "dhcp.pool_end":   args.pool_end,
        "dhcp.lease_file": args.lease_file,
        "dhcp.lease_time": args.lease_time,
        "dhcp.interface":  args.dhcp_iface,
        "tftp.root":       args.tftp_root,
        "tftp.port":       args.tftp_port,
        "tftp.interface":  args.tftp_iface,
        "pxe.boot_file":   args.boot_file,
    }
    return {k: v for k, v in mapping.items() if v is not None}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=LOG_FORMAT,
    )
    root_log = _make_logger("MAIN")

    if args.init:
        dest = Path(args.config)
        if dest.exists():
            root_log.warning(f"{dest} already exists — not overwriting. Delete it first.")
        else:
            dest.write_text(STARTER_TOML)
            root_log.info(f"Wrote starter config to {dest}")
        return

    config_path = Path(args.config)
    cfg = load_config(config_path, _cli_overrides(args))

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        root_log.info("Shutdown signal received, stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if args.serial_only:
        # Serial-only mode: no DHCP/TFTP
        if not args.serial:
            root_log.error("--serial-only requires --serial <port>")
            return
        root_log.info("Serial-only mode — Ctrl-C to stop")
        try:
            SerialMonitor(args.serial, args.serial_baud, stop_event).run()
        except KeyboardInterrupt:
            stop_event.set()
        return

    # Normal mode: DHCP + TFTP (+ optional serial monitor)
    dhcp_server = DHCPServer(cfg, stop_event)
    tftp_server = TFTPServer(cfg, stop_event)
    dhcp_thread = threading.Thread(target=dhcp_server.run, name="dhcp", daemon=True)
    tftp_thread = threading.Thread(target=tftp_server.run, name="tftp", daemon=True)
    dhcp_thread.start()
    tftp_thread.start()

    root_log.info("netboot.py running — Ctrl-C to stop")
    try:
        if args.serial:
            SerialMonitor(args.serial, args.serial_baud, stop_event).run()
        else:
            while not stop_event.is_set():
                stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        root_log.info("Ctrl-C received, stopping servers...")
        stop_event.set()
    dhcp_thread.join(timeout=3)
    tftp_thread.join(timeout=3)
    root_log.info("Bye.")

if __name__ == "__main__":
    main()
