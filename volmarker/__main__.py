"""Start VolMarker:  python -m volmarker --data <shared folder>"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import webbrowser

from .server import serve
from .service import Service
from .store import Store


def main() -> None:
    ap = argparse.ArgumentParser(prog="volmarker")
    ap.add_argument("--data", default=os.environ.get("VOLMARKER_DATA", "data"),
                    help="shared data folder (default: $VOLMARKER_DATA or ./data)")
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--user", default=getpass.getuser())
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    svc = Service(Store(args.data), args.user)
    svc.start_background()
    httpd = serve(svc, args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    logging.info("VolMarker for %s on %s (data: %s)", args.user, url, os.path.abspath(args.data))
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()


if __name__ == "__main__":
    main()
