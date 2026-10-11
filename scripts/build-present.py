#!/usr/bin/env python3
"""Build the Magpie web presentation from its template.

Inlines the Big Shoulders Display font and all images as data URIs so the
page is a single self-contained file. Emits:

  <repo>/present.html          (full document — open locally in any browser)
  <assets-dir>/../magpie-deck.html  (fragment for the Claude artifact)

Image sources are read from <assets-dir> (argv[1]). If the repo root contains
wall-hero.jpg it overrides the hero photo.
"""

import base64
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def data_uri(path: pathlib.Path) -> str:
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "woff2": "font/woff2"}[path.suffix.lstrip(".")]
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def find_asset(assets: pathlib.Path, name: str) -> pathlib.Path:
    # A repo-root image with the placeholder's name always wins, so photos can
    # be dropped in (e.g. wall-hero.jpg, wled.jpg) without touching the build.
    for base in (REPO, assets):
        for ext in ("jpg", "jpeg", "png"):
            p = base / f"{name}.{ext}"
            if p.exists():
                return p
    raise SystemExit(f"missing image asset: {name}")


def main() -> None:
    assets = pathlib.Path(sys.argv[1])
    template = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else assets.parent / "present2-template.html"
    tpl = template.read_text()

    font = data_uri(assets / "bsd-latin.woff2")
    tpl = tpl.replace(
        "{{FONTFACE}}",
        "@font-face{font-family:'Big Shoulders Display';font-style:normal;"
        "font-weight:600 700;font-display:swap;"
        f"src:url({font}) format('woff2');}}",
    )
    for name in set(re.findall(r"\{\{([a-z0-9-]+)\}\}", tpl)):
        tpl = tpl.replace("{{%s}}" % name, data_uri(find_asset(assets, name)))
    assert "{{" not in tpl, "unreplaced placeholder"

    out = REPO / "present.html"
    out.write_text(tpl)
    print(f"wrote {out} ({len(tpl) // 1024} KB)")

    frag = tpl
    frag = re.sub(r'<!doctype html>\s*<html[^>]*>\s*<head>\s*(<meta[^>]*>\s*)*', "", frag, count=1)
    frag = frag.replace("</head>\n<body>\n", "").replace("</body>\n</html>", "").rstrip("\n") + "\n"
    assert "<html" not in frag and "</html" not in frag
    frag_out = assets.parent / "magpie-deck.html"
    frag_out.write_bytes(frag.encode("ascii", "xmlcharrefreplace"))
    print(f"wrote {frag_out} ({frag_out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
