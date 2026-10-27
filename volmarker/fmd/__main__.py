"""Fenics Market Data feed.

    python -m volmarker.fmd feed     --data <shared folder> [--transport fix|webapi|xml]
    python -m volmarker.fmd simulate --data <shared folder> [--transport fix|webapi|xml] [--port N]
    python -m volmarker.fmd feed     --data <folder> --sim 127.0.0.1:8935   (feed from a simulator)

Connection settings live under "fmd" in <data>/config.json. The password
is never stored there: set FMD_PASSWORD, or point fmd.password_file at a
file on the local PC. xml = FMD 1.0 polled from the Fenics XML poster
(fmd.xml; password in FENICS_PASSWORD or fmd.xml.password_file).
"""

from __future__ import annotations

import argparse
import logging
import os

from ..store import Store
from . import simulator
from .feed import Feed, read_password, xml_url


def main() -> None:
    ap = argparse.ArgumentParser(prog="volmarker.fmd")
    ap.add_argument("command", choices=["feed", "simulate"])
    ap.add_argument("--data", default=os.environ.get("VOLMARKER_DATA", "data"))
    ap.add_argument("--port", type=int, help="simulator port (default 8935 FIX, 8936 WebAPI, 8937 XML)")
    ap.add_argument("--transport", choices=["fix", "webapi", "xml"], help="override fmd.transport")
    ap.add_argument("--sim", metavar="HOST:PORT",
                    help="feed: connect to a simulator instead of FMD (no TLS)")
    ap.add_argument("--force", action="store_true", help="feed: start even if another feed looks alive")
    ap.add_argument("--no-follow", action="store_true",
                    help="feed: do not publish FMD levels for pairs set to Follow FMD")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    store = Store(args.data)

    cfg = dict(store.config()["fmd"])
    if args.transport:
        cfg["transport"] = args.transport
    webapi, xml = cfg.get("transport") == "webapi", cfg.get("transport") == "xml"

    if args.command == "simulate":
        if xml:
            simulator.run_xml(store, args.port or 8937)
        elif webapi:
            simulator.run_webapi(store, args.port or 8936)
        else:
            simulator.run(store, args.port or 8935)
        return

    if args.sim:
        host, port = args.sim.rsplit(":", 1)
        if xml:
            cfg["xml"] = dict(cfg["xml"], url=f"http://{host}:{port}", username="sim")
        elif webapi:
            cfg["webapi"] = dict(cfg["webapi"], base_url=f"http://{host}:{port}",
                                 token_url=f"http://{host}:{port}/token", client_id="sim")
        else:
            cfg.update(host=host, port=int(port), tls=False,
                       sender_comp_id=cfg.get("sender_comp_id") or "SIMTEST")
        password = "sim"
    elif xml:
        if not (xml_url(cfg, store) and cfg["xml"].get("username")):
            raise SystemExit("Set fmd.xml.url (or weights_export.fenics_url) and fmd.xml.username in config.json")
        password = read_password(cfg)
    elif webapi:
        w = cfg["webapi"]
        if not (w.get("token_url") and w.get("client_id")):
            raise SystemExit("Set fmd.webapi.token_url and fmd.webapi.client_id in config.json")
        password = read_password(cfg)
    else:
        if not cfg.get("sender_comp_id"):
            raise SystemExit("Set fmd.sender_comp_id in config.json (provided by FMD)")
        password = read_password(cfg)
    Feed(store, cfg, password, force=args.force, follow=not args.no_follow).run()


if __name__ == "__main__":
    main()
