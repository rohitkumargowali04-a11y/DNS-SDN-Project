#!/usr/bin/env python3
"""Concurrent UDP DNS-like resolver; optional delay supports D2 experiments."""
import argparse
import json
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def load_zone(path):
    with open(path) as zone_file:
        records = json.load(zone_file)
    return {name.lower().rstrip("."): value for name, value in records.items()}


def build_response(data, zone, server_name):
    try:
        request = json.loads(data.decode("utf-8"))
        query_id = request["id"]
        query_name = request["name"].lower().rstrip(".")
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        return {"id": None, "rcode": "FORMERR", "server": server_name}

    record = zone.get(query_name)
    if record is None:
        return {"id": query_id, "rcode": "NXDOMAIN", "name": query_name,
                "server": server_name}
    return {"id": query_id, "rcode": "OK", "name": query_name,
            "ip": record["ip"], "ttl": record.get("ttl", 30),
            "server": server_name}


def handle(sock, data, address, zone, name, delay, stats, lock):
    response = build_response(data, zone, name)
    if delay > 0:
        time.sleep(delay)
    with lock:
        stats["queries"] += 1
    logging.info("query from %s:%d -> %s (%s)", address[0], address[1],
                 response.get("name"), response["rcode"])
    try:
        sock.sendto(json.dumps(response).encode("utf-8"), address)
    except OSError as error:
        logging.error("send failed to %s: %s", address, error)


def main():
    parser = argparse.ArgumentParser(description="UDP DNS-like resolver")
    parser.add_argument("--name", default="dns1")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5353)
    parser.add_argument("--zone", default="zone.json")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="artificial processing delay in seconds for experiments")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [" + args.name + "] %(message)s")
    zone = load_zone(args.zone)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(1.0)

    stats = {"queries": 0}
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=args.workers)
    logging.info("listening on %s:%d; records=%d; delay=%.3fs",
                 args.bind, args.port, len(zone), args.delay)
    try:
        while True:
            try:
                data, address = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as error:
                logging.error("receive error: %s", error)
                continue
            pool.submit(handle, sock, data, address, zone, args.name,
                        args.delay, stats, lock)
    except KeyboardInterrupt:
        pass
    finally:
        logging.info("shutting down; served %d queries", stats["queries"])
        pool.shutdown(wait=False)
        sock.close()


if __name__ == "__main__":
    main()
