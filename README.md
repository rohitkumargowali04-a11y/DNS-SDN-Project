# Distributed DNS-Like Name Resolution System (UDP + SDN)

Computer Networks (UE25CS243A) – D2 update. This version replaces the D1
Round Robin resolver selection with **Least Response Time (Latency-Based Load
Balancing)** while retaining the UDP DNS-like protocol, VIP, Mininet topology,
TTL cache, and POX/OpenFlow forwarding.

## 1. D2 objective

The POX controller measures the time between forwarding a DNS request to a
resolver and receiving its response. It keeps an Exponential Weighted Moving
Average (EWMA) for each resolver and normally selects the resolver with the
lowest EWMA latency. It also periodically samples the least recently measured
resolver so that measurements remain current.

The controller counts unanswered requests. After two consecutive unanswered
requests, it temporarily excludes that resolver for 10 seconds. It then makes
the resolver eligible again for a recovery test. This is request/response-based
failure detection, not a separate proactive heartbeat service.

## 2. Architecture

- h1: client 10.0.0.1
- h2: client 10.0.0.2
- h3 / dns1: resolver 10.0.0.3, switch port 3
- h4 / dns2: resolver 10.0.0.4, switch port 4
- s1: Open vSwitch, OpenFlow 1.0
- POX: controller at 127.0.0.1:6633
- VIP: 10.0.0.100:5353, virtual MAC 00:00:00:00:00:64

For latency measurement, DNS response packets are sent to the controller by
the reverse OpenFlow rule. The controller records RTT, rewrites the response
source to the VIP, and sends it to the client. This adds controller work to
response forwarding but allows the controller to measure each request.

## 3. Files

| File | Purpose |
|---|---|
| `dns_sdn_controller.py` | EWMA latency measurement, least-response-time selection, timeout tracking and failover |
| `dns_client.py` | UDP queries, TTL cache, fresh source port per retry and latency summary |
| `dns_server.py` | Concurrent UDP resolver; optional artificial delay for experiments |
| `topo.py` | Mininet topology; supports per-resolver delay arguments |
| `zone.json` | Name-to-IP records |

## 4. Setup

Install Mininet, Open vSwitch and Git if not already installed:

```bash
sudo apt update
sudo apt install mininet openvswitch-switch git python3 -y
```

Get POX if it is not already installed:

```bash
git clone https://github.com/noxrepo/pox ~/pox
```

Put this project directory at `~/dns-sdn`. Link the controller into POX:

```bash
ln -sf ~/dns-sdn/dns_sdn_controller.py ~/pox/ext/dns_sdn_controller.py
```

## 5. Run

**Terminal 1 – POX controller:**

```bash
cd ~/pox
./pox.py log.level --DEBUG dns_sdn_controller
```

**Terminal 2 – Mininet topology:**

```bash
cd ~/dns-sdn
sudo mn -c
sudo python3 topo.py
```

Start the controller before starting Mininet. The topology starts both DNS
servers automatically. At the `mininet>` prompt, run:

```text
h1 python3 dns_client.py --no-cache --repeat 12 www.pes.local
```

Look at Terminal 1 for `SELECT` and `LATENCY` messages. The first requests
measure both resolvers; subsequent selections should normally prefer the
resolver with the lower EWMA response time, with periodic exploration.

## 6. Demonstrate latency-aware selection

Stop the topology with `exit`, clean Mininet, then restart it with different
server processing delays:

```bash
sudo mn -c
sudo python3 topo.py --dns1-delay 0.150 --dns2-delay 0.020
```

Then, inside Mininet:

```text
h1 python3 dns_client.py --no-cache --repeat 15 www.pes.local
```

DNS2 should normally become preferred after both servers have been sampled.
The measured times include network, controller, and server processing time;
they are not just the artificial delay value.

## 7. Test cases

| Test | Command / action | Expected observation |
|---|---|---|
| T1 – resolution | `h1 python3 dns_client.py www.pes.local` | `192.168.10.10` returned |
| T2 – NXDOMAIN | `h1 python3 dns_client.py missing.pes.local` | `NXDOMAIN` |
| T3 – cache | `h1 python3 dns_client.py www.pes.local www.pes.local` | second request is a cache hit |
| T4 – latency selection | Start with `--dns1-delay 0.150 --dns2-delay 0.020`; run `h1 python3 dns_client.py --no-cache --repeat 15 www.pes.local` | after both samples, lower-latency DNS2 is normally selected |
| T5 – reverse experiment | Start with `--dns1-delay 0.020 --dns2-delay 0.150` | DNS1 should normally be preferred after measurement |
| T6 – second client | `h2 python3 dns_client.py --no-cache --repeat 6 cn.pes.local` | second client also uses VIP and controller selection |
| T7 – flow inspection | `sh ovs-ofctl dump-flows s1` | DNS forward rules and reverse-to-controller rules appear |
| T8 – failure | `h3 pkill -f dns_server.py` then run repeated uncached queries from h1 | unanswered DNS1 requests are counted; DNS1 is temporarily avoided after threshold; DNS2 serves requests |
| T9 – recovery | Restart DNS1 from Mininet with `h3 python3 ~/dns-sdn/dns_server.py --name dns1 --bind 10.0.0.3 --port 5353 --zone ~/dns-sdn/zone.json > /tmp/dns1.log 2>&1 &` and wait for cooldown | DNS1 becomes eligible again and is sampled |
| T10 – malformed packet | use the D1 malformed UDP test | resolver returns `FORMERR` |

To inspect resolver logs:

```text
h3 cat /tmp/dns1.log
h4 cat /tmp/dns2.log
```

## 8. Important parameters

At the top of `dns_sdn_controller.py`:

- `EWMA_ALPHA = 0.30`: weight of the newest response time.
- `RESPONSE_TIMEOUT = 2.0`: time before a request is counted unanswered.
- `FAILURE_THRESHOLD = 2`: consecutive misses before temporary exclusion.
- `DOWN_COOLDOWN = 10.0`: temporary exclusion duration.
- `PROBE_EVERY = 5`: periodic selection to refresh the less-recently measured resolver.

The client timeout defaults to 2.5 seconds and retries four times. Keep the
client timeout longer than the controller's response timeout so the controller
can register an unanswered request before the next retry.

## 9. Limitations / interpretation

- This is a small experimental load balancer, not a production DNS service.
- RTT is measured per query and includes controller forwarding overhead.
- The controller sees every DNS reply to measure it, which can become a
  bottleneck at high request rates.
- Failure detection is based on unanswered DNS requests; it is not an
  independent heartbeat or physical-link detector.
- A recovered resolver is tested when its cooldown expires and it becomes
  eligible for selection again.
- Compare multiple repetitions and report averages; one run is not enough to
  claim a performance improvement.
