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
    else:
        verdict = ("The resolver is reachable but is not answering for these names. A filtering "
                   "DNS server such as Pi-hole is the usual cause.")

    return {"checks": checks, "verdict": verdict,
            "proxy_env": {k: v for k, v in os.environ.items() if "proxy" in k.lower()}}
