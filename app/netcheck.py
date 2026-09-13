"""Network diagnostics, for when metadata lookups fail and there is no shell handy.

Each probe isolates one layer, so the first failure tells you where to look:
resolver configured → resolver reachable → names resolve → traffic leaves.
"""
from __future__ import annotations

import os
import socket

import httpx


def _resolv_conf() -> dict:
    try:
        with open("/etc/resolv.conf") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except OSError as exc:
        return {"ok": False, "detail": f"Could not read /etc/resolv.conf: {exc}"}
    servers = [l.split()[1] for l in lines if l.startswith("nameserver") and len(l.split()) > 1]
    if not servers:
        return {"ok": False, "detail": "No nameserver is configured inside the container."}
    return {"ok": True, "detail": "Configured resolvers: " + ", ".join(servers),
            "servers": servers}


def _port_open(host: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolver_reachable(servers: list[str]) -> dict:
    if not servers:
        return {"ok": False, "detail": "No resolver to test."}
    reached = [s for s in servers if _port_open(s, 53)]
    if reached:
        return {"ok": True, "detail": "Reached DNS on " + ", ".join(reached)}
    return {"ok": False,
            "detail": "None of the configured resolvers answered on port 53: "
                      + ", ".join(servers)}


def _raw_dns(server: str, name: str = "musicbrainz.org", timeout: float = 4.0) -> bool:
    """Ask an upstream resolver directly, bypassing whatever the container is using.

    Distinguishes 'Docker is not forwarding' from 'port 53 cannot leave the NAS',
    which need completely different fixes.
    """
    query = bytearray(b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    for label in name.split("."):
        query.append(len(label))
        query += label.encode()
    query += b"\x00\x00\x01\x00\x01"  # root terminator, type A, class IN
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(bytes(query), (server, 53))
        reply, _ = sock.recvfrom(512)
        sock.close()
        return len(reply) > 12 and reply[0:2] == b"\x00\x01"
    except OSError:
        return False


def _upstream_dns() -> dict:
    working = [s for s in ("1.1.1.1", "8.8.8.8") if _raw_dns(s)]
    if working:
        return {"ok": True,
                "detail": "Public DNS answers directly on " + ", ".join(working)
                          + ". So port 53 is open and the container is simply not "
                            "forwarding to it."}
    return {"ok": False,
            "detail": "Neither 1.1.1.1 nor 8.8.8.8 answered on UDP port 53. Something "
                      "between the NAS and the internet is blocking DNS traffic."}


def _resolves() -> dict:
    names = ["musicbrainz.org", "api.discogs.com"]
    out, failed = [], []
    for name in names:
        try:
            out.append(f"{name} → {socket.gethostbyname(name)}")
        except OSError as exc:
            failed.append(f"{name}: {exc}")
    if failed:
        return {"ok": False, "detail": "; ".join(failed)}
    return {"ok": True, "detail": "; ".join(out)}


def _egress() -> dict:
    """Talk to an IP directly, so a pass here rules DNS in as the only problem."""
    try:
        with httpx.Client(timeout=8, verify=False) as client:
            r = client.get("https://1.1.1.1/", headers={"User-Agent": "Harmon/1.0"})
        return {"ok": True, "detail": f"Outbound HTTPS works (answered {r.status_code})."}
    except Exception as exc:
        return {"ok": False,
                "detail": f"Could not reach 1.1.1.1 on port 443 either: {str(exc)[:150]}"}


def run() -> dict:
    conf = _resolv_conf()
    reach = _resolver_reachable(conf.get("servers", []))
    resolves = _resolves()
    egress = _egress()
    upstream = _upstream_dns() if not resolves["ok"] else {"ok": True,
                                                          "detail": "Not needed — names already resolve."}

    # If names actually resolve, a failed port-53 probe is noise: plenty of
    # setups route DNS through something that refuses a bare TCP connection.
    if resolves["ok"] and not reach["ok"]:
        reach = {"ok": True,
                 "detail": "Not directly reachable on port 53, but resolution works, "
                           "so something is handling it."}

    checks = [
        {"name": "A resolver is configured", **conf},
        {"name": "That resolver answers", **reach},
        {"name": "Domain names resolve", **resolves},
        {"name": "Traffic leaves the NAS", **egress},
        {"name": "Public DNS answers directly", **upstream},
    ]
    for c in checks:
        c.pop("servers", None)

    if resolves["ok"] and egress["ok"]:
        verdict = "Networking is fine. Any remaining failures are key or rate-limit problems."
    elif not egress["ok"] and not resolves["ok"]:
        verdict = ("Nothing is getting out of the container at all. Check that the NAS itself "
                   "has internet access and that its firewall is not blocking outbound traffic.")
    elif not egress["ok"]:
        verdict = ("Names resolve but outbound HTTPS is blocked. Look at the NAS firewall or "
                   "anything filtering traffic on the way out.")
    elif not conf["ok"]:
        verdict = ("No DNS server reached the container. Add a dns: block to the compose file "
                   "and recreate the container rather than restarting it.")
    elif not reach["ok"]:
        verdict = ("The DNS servers in the compose file cannot be reached from the container. "
                   "Confirm the container was recreated after you added them, not just "
                   "restarted, and try your router's address instead.")
    elif upstream["ok"]:
        verdict = ("Docker's built-in resolver is not passing queries upstream. The dns: block "
                   "only takes effect when a container is created, so delete the container "
                   "and deploy it again rather than restarting it. If that does not take, set "
                   "the DNS on the NAS itself under Control Panel, Network, General.")
    else:
        verdict = ("DNS traffic cannot leave the network, even though HTTPS can. A router or "
                   "ISP that intercepts port 53 is the usual cause. Pointing the container at "
                   "your router's own address normally works, since that is allowed to resolve.")

    return {"checks": checks, "verdict": verdict,
            "proxy_env": {k: v for k, v in os.environ.items() if "proxy" in k.lower()}}
