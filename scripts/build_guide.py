"""Build the one-file user guide, docs/VolMarker-guide.html, from the guide VolMarker serves.

    python scripts/build_guide.py [--out docs/VolMarker-guide.html] [--pdf]

The guide's source is volmarker/static/guide/index.html with its screenshots in shots/
beside it; the app shows it under Help (/guide/). This script writes a copy with the
screenshots embedded, to send or keep as one file. Some mail and web filters block such
a file (an HTML page carrying images inside it); the app's Help page works everywhere.

--pdf also prints docs/VolMarker-guide.pdf with Chrome or Edge (headless), if one is
installed. Printing the Help page to PDF from the browser gives the same.
"""

from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "volmarker" / "static" / "guide"
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def inline(html: str) -> str:
    def img(m: re.Match) -> str:
        data = (SRC / m.group(1)).read_bytes()
        return f'src="data:image/png;base64,{base64.b64encode(data).decode()}"'
    return re.sub(r'src="(shots/[^"]+\.png)"', img, html)


def browser() -> str | None:
    for b in BROWSERS + [shutil.which(n) for n in ("google-chrome", "chromium", "chrome", "msedge")]:
        if b and Path(b).exists():
            return b
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "docs" / "VolMarker-guide.html"))
    ap.add_argument("--pdf", action="store_true", help="also print the PDF with Chrome or Edge")
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(inline((SRC / "index.html").read_text(encoding="utf-8")), encoding="utf-8")
    print(f"Wrote {out}")
    if a.pdf:
        b = browser()
        if not b:
            sys.exit("No Chrome or Edge found: open the Help page and print it to PDF instead")
        pdf = out.with_suffix(".pdf")
        subprocess.run([b, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf}", out.resolve().as_uri()], check=True, capture_output=True)
        print(f"Wrote {pdf}")


if __name__ == "__main__":
    main()
