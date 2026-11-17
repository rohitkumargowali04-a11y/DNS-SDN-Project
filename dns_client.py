#!/usr/bin/env python3
"""DNS-like UDP client with TTL cache, retries and latency reporting.

Each attempt uses a fresh UDP source port so the SDN controller can make a new
resolver-selection decision after a timeout/failover.
"""
import argparse
import json
import random
import socket
import statistics
import time


class Cache:
    def __init__(self):
        self._d = {}

    def get(self, name):
        entry = self._d.get(name)
        if entry and entry[1] > time.time():
            return entry[0]
        self._d.pop(name, None)
        return None

    def put(self, name, ip, ttl):
        self._d[name] = (ip, time.time() + ttl)


def query(name, server, port, timeout, retries):
    for attempt in range(1, retries + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        query_id = random.randint(0, 65535)
        message = json.dumps({"id": query_id, "type": "A", "name": name}).encode()
        started = time.perf_counter()
        try:
            sock.sendto(message, (server, port))
            while True:
                data, _ = sock.recvfrom(4096)
                try:
                    response = json.loads(data.decode())
                except (ValueError, UnicodeDecodeError):
                    continue
                if response.get("id") == query_id:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    return response, elapsed_ms, attempt
        except socket.timeout:
            print("  [timeout] attempt %d/%d for %s" % (attempt, retries, name))
        except OSError as exc:
            print("  [socket error] %s" % exc)
        finally:
            sock.close()
    return None, None, retries


def main():
    parser = argparse.ArgumentParser(description="DNS-like client using the SDN virtual resolver")
    parser.add_argument("names", nargs="+")
    parser.add_argument("--server", default="10.0.0.100", help="resolver or virtual IP")
    parser.add_argument("--port", type=int, default=5353)
    parser.add_argument("--timeout", type=float, default=2.5,
                         help="seconds to wait per attempt; keep above controller response timeout")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    cache = Cache()
    successful_latencies = []
    failures = 0
    cache_hits = 0
    total_queries = 0

    for _ in range(args.repeat):
        for raw_name in args.names:
            name = raw_name.lower().rstrip(".")
            if not args.no_cache:
                cached_ip = cache.get(name)
                if cached_ip:
                    cache_hits += 1
                    print("%-22s -> %-15s [CACHE HIT]" % (name, cached_ip))
                    continue

            total_queries += 1
            response, elapsed_ms, attempts = query(
                name, args.server, args.port, args.timeout, args.retries)
            if response is None:
                failures += 1
                print("%-22s -> FAILED after %d attempts" % (name, attempts))
            elif response.get("rcode") == "OK":
                successful_latencies.append(elapsed_ms)
                cache.put(name, response["ip"], response.get("ttl", 30))
                print("%-22s -> %-15s via %-5s %7.2f ms (attempt %d)" %
                      (name, response["ip"], response.get("server", "?"), elapsed_ms, attempts))
            else:
                successful_latencies.append(elapsed_ms)
                print("%-22s -> %s via %s %.2f ms" %
                      (name, response.get("rcode", "ERROR"),
                       response.get("server", "?"), elapsed_ms))
            time.sleep(args.interval)

    print("\n--- summary ---")
    print("network queries: %d  cache hits: %d  failures: %d" %
          (total_queries, cache_hits, failures))
    if successful_latencies:
        print("successful response latency ms: min=%.2f avg=%.2f max=%.2f" %
              (min(successful_latencies), statistics.mean(successful_latencies),
               max(successful_latencies)))


if __name__ == "__main__":
    main()
