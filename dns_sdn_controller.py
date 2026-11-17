"""POX controller for DNS-aware, least-response-time SDN load balancing.

The controller measures resolver RTT by sending DNS replies to the controller
(OpenFlow PacketIn), updates an EWMA latency estimate, and forwards the reply
to the original client after restoring the virtual resolver IP/MAC.

Selection policy:
  * Initially sample each available resolver.
  * Normally choose the resolver with the lowest EWMA response time.
  * Every PROBE_EVERY requests, sample the least-recently measured resolver.
  * After FAILURE_THRESHOLD unanswered requests, temporarily mark a resolver
    unavailable. It becomes eligible again after DOWN_COOLDOWN seconds.

Run: cd ~/pox && ./pox.py log.level --DEBUG dns_sdn_controller
OpenFlow 1.0.
"""
from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.packet.ethernet import ethernet
from pox.lib.packet.arp import arp
from pox.lib.addresses import IPAddr, EthAddr
from pox.lib.util import dpid_to_str
from pox.lib.recoco import Timer
import time

log = core.getLogger()

VIP = IPAddr("10.0.0.100")
VMAC = EthAddr("00:00:00:00:00:64")
DNS_PORT = 5353
FLOW_IDLE = 5
DNS_PRIORITY = 40000

# Tunable D2 parameters
EWMA_ALPHA = 0.30
RESPONSE_TIMEOUT = 2.0       # pending query considered unanswered after this
FAILURE_THRESHOLD = 2        # consecutive unanswered queries before cooldown
DOWN_COOLDOWN = 10.0         # seconds before a failed resolver is tried again
PROBE_EVERY = 5              # periodically refresh the other resolver's sample

# Must match fixed switch ports and addresses in topo.py.
RESOLVERS = [
    {"name": "dns1", "ip": IPAddr("10.0.0.3"),
     "mac": EthAddr("00:00:00:00:00:03"), "port": 3},
    {"name": "dns2", "ip": IPAddr("10.0.0.4"),
     "mac": EthAddr("00:00:00:00:00:04"), "port": 4},
]
BY_IP = {r["ip"]: r for r in RESOLVERS}


class DnsSdn(object):
    def __init__(self, connection):
        self.connection = connection
        self.mac_to_port = {}
        self.pending = {}  # (client IP string, client UDP source port) -> query info
        self.selection_count = 0
        self.tie_breaker = 0
        self.counts = {}
        now = time.time()
        for r in RESOLVERS:
            r.update({"alive": True, "latency_ms": None, "samples": 0,
                      "misses": 0, "down_until": 0.0, "last_sample": now,
                      "assigned": 0})
            self.counts[r["name"]] = 0
        connection.addListeners(self)
        self.expiry_timer = Timer(0.5, self._expire_pending, recurring=True)
        log.info("Switch %s connected; least-response-time balancing enabled",
                 dpid_to_str(connection.dpid))

    # ---------- resolver selection ----------
    def _refresh_health(self):
        now = time.time()
        for r in RESOLVERS:
            if not r["alive"] and now >= r["down_until"]:
                r["alive"] = True
                r["misses"] = 0
                r["latency_ms"] = None  # re-measure after cooldown
                r["samples"] = 0
                log.info("%s cooldown ended; resolver eligible for a recovery test", r["name"])

    def _pick(self):
        self._refresh_health()
        available = [r for r in RESOLVERS if r["alive"]]
        if not available:
            return None

        self.selection_count += 1
        unmeasured = [r for r in available if r["latency_ms"] is None]
        if unmeasured:
            # Ensure each resolver gets an initial sample before comparing them.
            min_assigned = min(r["assigned"] for r in unmeasured)
            candidates = [r for r in unmeasured if r["assigned"] == min_assigned]
            chosen = candidates[self.tie_breaker % len(candidates)]
            self.tie_breaker += 1
            return chosen

        if len(available) > 1 and self.selection_count % PROBE_EVERY == 0:
            # Keep estimates fresh by occasionally testing the least recently sampled.
            return min(available, key=lambda r: r["last_sample"])

        return min(available, key=lambda r: (r["latency_ms"], r["assigned"]))

    def _record_success(self, resolver, latency_ms):
        if resolver["latency_ms"] is None:
            resolver["latency_ms"] = latency_ms
        else:
            resolver["latency_ms"] = (EWMA_ALPHA * latency_ms +
                                      (1.0 - EWMA_ALPHA) * resolver["latency_ms"])
        resolver["samples"] += 1
        resolver["misses"] = 0
        resolver["last_sample"] = time.time()
        resolver["alive"] = True
        log.info("LATENCY %s latest=%.2f ms EWMA=%.2f ms samples=%d",
                 resolver["name"], latency_ms, resolver["latency_ms"],
                 resolver["samples"])

    def _expire_pending(self):
        now = time.time()
        expired = [key for key, value in self.pending.items()
                   if now - value["started"] >= RESPONSE_TIMEOUT]
        for key in expired:
            info = self.pending.pop(key, None)
            if info is None:
                continue
            r = info["resolver"]
            r["misses"] += 1
            log.warning("No response from %s for client %s (miss %d/%d)",
                        r["name"], info["client_ip"], r["misses"], FAILURE_THRESHOLD)
            if r["misses"] >= FAILURE_THRESHOLD:
                r["alive"] = False
                r["down_until"] = now + DOWN_COOLDOWN
                log.warning("FAILOVER: %s temporarily unavailable for %.1f seconds",
                            r["name"], DOWN_COOLDOWN)

    # ---------- OpenFlow helpers ----------
    def _flood(self, event):
        msg = of.ofp_packet_out()
        msg.data = event.ofp
        msg.in_port = event.port
        msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
        self.connection.send(msg)

    def _send_flow(self, match, actions, data=None):
        fm = of.ofp_flow_mod()
        fm.match = match
        fm.idle_timeout = FLOW_IDLE
        fm.priority = DNS_PRIORITY
        fm.actions.extend(actions)
        if data is not None:
            fm.data = data
        self.connection.send(fm)

    # ---------- packet handling ----------
    def _handle_PacketIn(self, event):
        packet = event.parsed
        if not packet.parsed:
            return
        self.mac_to_port[packet.src] = event.port

        a = packet.find("arp")
        if a is not None and a.opcode == arp.REQUEST and a.protodst == VIP:
            self._arp_reply(event, packet, a)
            return

        ip = packet.find("ipv4")
        udp = packet.find("udp")
        if ip is not None and udp is not None:
            if ip.dstip == VIP and udp.dstport == DNS_PORT:
                self._dns_query(event, ip, udp)
                return
            if ip.srcip in BY_IP and udp.srcport == DNS_PORT:
                self._dns_reply(event, packet, ip, udp)
                return

        self._l2(event, packet)

    def _arp_reply(self, event, packet, request):
        reply = arp()
        reply.opcode = arp.REPLY
        reply.hwsrc = VMAC
        reply.hwdst = request.hwsrc
        reply.protosrc = VIP
        reply.protodst = request.protosrc
        frame = ethernet(type=ethernet.ARP_TYPE, src=VMAC, dst=packet.src)
        frame.payload = reply
        msg = of.ofp_packet_out()
        msg.data = frame.pack()
        msg.in_port = event.port
        msg.actions.append(of.ofp_action_output(port=of.OFPP_IN_PORT))
        self.connection.send(msg)

    def _reply_capture_rule(self, resolver, client_ip, client_port, source_port):
        """Send each matching DNS response to POX so its RTT can be measured."""
        match = of.ofp_match(dl_type=0x0800, nw_proto=17,
                             in_port=resolver["port"], nw_src=resolver["ip"],
                             nw_dst=client_ip, tp_src=DNS_PORT,
                             tp_dst=source_port)
        actions = [of.ofp_action_output(port=of.OFPP_CONTROLLER, max_len=0xffff)]
        self._send_flow(match, actions)

    def _dns_query(self, event, ip, udp):
        resolver = self._pick()
        if resolver is None:
            log.error("No available DNS resolver; dropping query from %s", ip.srcip)
            return

        client_ip = str(ip.srcip)
        client_port = int(udp.srcport)
        key = (client_ip, client_port)
        self.pending[key] = {"resolver": resolver, "started": time.time(),
                             "client_ip": ip.srcip, "client_port": event.port}
        resolver["assigned"] += 1
        self.counts[resolver["name"]] += 1

        latency = resolver["latency_ms"]
        latency_text = "unmeasured" if latency is None else "%.2f ms" % latency
        log.info("SELECT %s for %s:%d (EWMA %s)", resolver["name"],
                 client_ip, client_port, latency_text)

        # Install the reverse capture rule first so a fast response cannot race
        # the rule installation. The controller needs the reply to measure RTT.
        self._reply_capture_rule(resolver, ip.srcip, event.port, udp.srcport)

        # Forward query: client -> VIP becomes client -> selected resolver.
        match = of.ofp_match(dl_type=0x0800, nw_proto=17, in_port=event.port,
                             nw_src=ip.srcip, nw_dst=VIP,
                             tp_src=udp.srcport, tp_dst=DNS_PORT)
        actions = [of.ofp_action_dl_addr.set_dst(resolver["mac"]),
                   of.ofp_action_nw_addr.set_dst(resolver["ip"]),
                   of.ofp_action_output(port=resolver["port"])]
        self._send_flow(match, actions, data=event.ofp)

    def _dns_reply(self, event, packet, ip, udp):
        """Measure the resolver RTT, then restore VIP source and send to client."""
        key = (str(ip.dstip), int(udp.dstport))
        info = self.pending.pop(key, None)
        client_port = info["client_port"] if info else self.mac_to_port.get(packet.dst)
        if client_port is None:
            log.warning("Cannot locate client port for DNS reply from %s", ip.srcip)
            return

        if info is not None:
            latency_ms = (time.time() - info["started"]) * 1000.0
            self._record_success(info["resolver"], latency_ms)

        # The reply packet is sent to the controller by the reverse flow rule.
        # Rewrite it to look as though it came from the virtual resolver.
        msg = of.ofp_packet_out()
        msg.data = event.ofp
        msg.in_port = event.port
        msg.actions.extend([
            of.ofp_action_dl_addr.set_src(VMAC),
            of.ofp_action_nw_addr.set_src(VIP),
            of.ofp_action_output(port=client_port),
        ])
        self.connection.send(msg)

    def _l2(self, event, packet):
        port = self.mac_to_port.get(packet.dst)
        if port is None or packet.dst.is_multicast:
            self._flood(event)
            return
        if port == event.port:
            return
        fm = of.ofp_flow_mod()
        fm.match = of.ofp_match.from_packet(packet, event.port)
        fm.idle_timeout = 10
        fm.hard_timeout = 30
        fm.actions.append(of.ofp_action_output(port=port))
        fm.data = event.ofp
        self.connection.send(fm)


def launch():
    def start(event):
        DnsSdn(event.connection)
    core.openflow.addListenerByName("ConnectionUp", start)
    log.info("DNS SDN controller running: VIP %s:%d, least-response-time policy",
             VIP, DNS_PORT)
