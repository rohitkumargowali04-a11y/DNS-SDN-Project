#!/usr/bin/env python3
"""Mininet topology for the DNS-like system and D2 latency experiments.

Fixed switch ports: h1=1, h2=2, dns1/h3=3, dns2/h4=4.
Start POX before running this script.
"""
import argparse
import os
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info

BASE = os.path.dirname(os.path.abspath(__file__))
HOSTS = [
    ("h1", "10.0.0.1/24", "00:00:00:00:00:01", 1),
    ("h2", "10.0.0.2/24", "00:00:00:00:00:02", 2),
    ("h3", "10.0.0.3/24", "00:00:00:00:00:03", 3),
    ("h4", "10.0.0.4/24", "00:00:00:00:00:04", 4),
]


def run(args):
    net = Mininet(controller=None, switch=OVSKernelSwitch, build=False)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6633)
    switch = net.addSwitch("s1", protocols="OpenFlow10")
    for name, ip, mac, port in HOSTS:
        host = net.addHost(name, ip=ip, mac=mac)
        net.addLink(host, switch, port2=port)

    net.start()
    net.waitConnected()

    for host_name, resolver_name, delay in (
            ("h3", "dns1", args.dns1_delay),
            ("h4", "dns2", args.dns2_delay)):
        host = net.get(host_name)
        host.cmd("python3 %s/dns_server.py --name %s --bind %s --port 5353 "
                 "--zone %s/zone.json --delay %.3f > /tmp/%s.log 2>&1 &" %
                 (BASE, resolver_name, host.IP(), BASE, delay, resolver_name))

    info("*** DNS1 artificial delay: %.3f s; DNS2 artificial delay: %.3f s\n" %
         (args.dns1_delay, args.dns2_delay))
    info("*** Resolver logs: h3 cat /tmp/dns1.log | h4 cat /tmp/dns2.log\n")
    info("*** Test: h1 python3 %s/dns_client.py --no-cache --repeat 12 www.pes.local\n" % BASE)
    CLI(net)
    net.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DNS SDN Mininet topology")
    parser.add_argument("--dns1-delay", type=float, default=0.0,
                        help="artificial DNS1 processing delay in seconds")
    parser.add_argument("--dns2-delay", type=float, default=0.0,
                        help="artificial DNS2 processing delay in seconds")
    arguments = parser.parse_args()
    setLogLevel("info")
    run(arguments)
