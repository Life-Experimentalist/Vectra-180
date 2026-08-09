#!/usr/bin/env python3
"""Draw the social preview card, site/og.png.

Link previews on most platforms will not render an SVG, so the one raster
asset in this repository is generated rather than hand-drawn -- this script is
its source. It uses only NumPy and OpenCV, which the project already depends
on, so there is nothing extra to install.

    python assets/make-og.py

Re-run it if the tagline or the version on the card ever changes.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

W, H = 1200, 630

BG_TOP = (46, 26, 16)  # BGR
BG_BOTTOM = (18, 10, 7)
CYAN = (254, 242, 0)
BLUE = (254, 172, 79)
GRID = (64, 38, 30)
TEXT = (247, 237, 232)
MUTED = (184, 163, 148)
DIM = (144, 122, 100)

FONT = cv2.FONT_HERSHEY_DUPLEX
FONT_THIN = cv2.FONT_HERSHEY_SIMPLEX

OUT = Path(__file__).resolve().parent.parent / "site" / "og.png"


def background() -> np.ndarray:
    ramp = np.linspace(0.0, 1.0, H, dtype=np.float32)[:, None, None]
    top = np.array(BG_TOP, dtype=np.float32)
    bottom = np.array(BG_BOTTOM, dtype=np.float32)
    img = top * (1.0 - ramp) + bottom * ramp
    img = np.repeat(img, W, axis=1)

    # A cyan bloom in the top right, the same one the site header carries.
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    glow = np.exp(-(((xx - 980) / 620.0) ** 2 + ((yy - 40) / 420.0) ** 2))
    img += np.array(CYAN, dtype=np.float32) * glow[..., None] * 0.16

    return np.clip(img, 0, 255)


def grid(img: np.ndarray) -> None:
    layer = np.zeros_like(img)
    for x in range(0, W, 48):
        cv2.line(layer, (x, 0), (x, H), GRID, 1, cv2.LINE_AA)
    for y in range(0, H, 48):
        cv2.line(layer, (0, y), (W, y), GRID, 1, cv2.LINE_AA)

    # Fade it out toward the bottom so the card does not read as graph paper.
    fade = np.linspace(1.0, 0.0, H, dtype=np.float32)[:, None, None] ** 1.6
    img += layer * fade


def lenses(img: np.ndarray) -> None:
    for cx in (196, 372):
        cv2.circle(img, (cx, 312), 96, GRID, 7, cv2.LINE_AA)
        cv2.circle(img, (cx, 312), 68, BLUE, 3, cv2.LINE_AA)
        cv2.circle(img, (cx, 312), 40, CYAN, 2, cv2.LINE_AA)
        cv2.circle(img, (cx, 312), 11, CYAN, -1, cv2.LINE_AA)
    # The horizon the two share -- the thing the whole project is pointed at.
    cv2.line(img, (76, 312), (492, 312), CYAN, 1, cv2.LINE_AA)


def text(img: np.ndarray) -> None:
    x = 560

    (w_vectra, _), _ = cv2.getTextSize("VECTRA", FONT, 3.1, 6)
    cv2.putText(img, "VECTRA", (x, 300), FONT, 3.1, TEXT, 6, cv2.LINE_AA)
    cv2.putText(img, "180", (x + w_vectra + 6, 300), FONT, 3.1, CYAN, 6, cv2.LINE_AA)

    cv2.line(img, (x + 3, 336), (1120, 336), GRID, 2, cv2.LINE_AA)

    cv2.putText(img, "DUAL-FISHEYE DASHCAM", (x + 3, 386), FONT_THIN, 0.86, MUTED, 1, cv2.LINE_AA)
    cv2.putText(img, "FOR THE RASPBERRY PI CM5", (x + 3, 420), FONT_THIN, 0.86, MUTED, 1, cv2.LINE_AA)

    cv2.putText(
        img,
        "LOOP RECORD // INCIDENT LOCK // DEPTH ON DEMAND",
        (x + 3, 476),
        FONT_THIN,
        0.6,
        CYAN,
        1,
        cv2.LINE_AA,
    )

    cv2.putText(img, "v1.0.0  Apache-2.0", (x + 3, 540), FONT_THIN, 0.56, DIM, 1, cv2.LINE_AA)


def main() -> None:
    img = background()
    grid(img)

    # The gradient and the grid are float work; everything from here is drawing,
    # and putText only accepts 8-bit.
    img = np.clip(img, 0, 255).astype(np.uint8)

    lenses(img)
    text(img)

    # A hairline border, so the card does not bleed into a dark timeline.
    cv2.rectangle(img, (0, 0), (W - 1, H - 1), GRID, 2, cv2.LINE_AA)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(OUT), img)
    print(f"wrote {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()
