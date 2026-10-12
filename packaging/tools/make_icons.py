"""Generate the FPK icon PNGs from the 音迹 favicon mark (standard library only).

The mark: red rounded square (#d92d24) with five cream waveform bars
(#fff5e8), identical to web/assets/icon.svg. Rendered at 4x and
box-downsampled for smooth edges; corners are transparent. Outputs the sizes
fnOS expects: ICON.PNG (64), ICON_256.PNG (256), ui images 64/256.
"""
import struct
import zlib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "fnmusic-journal"
SUPERSAMPLE = 4
BACKGROUND = (217, 45, 36)   # #d92d24
BARS = (255, 245, 232)       # #fff5e8
# favicon geometry in its 64-unit viewBox: bar centers, height, stroke width
BAR_CENTERS = (12, 22, 32, 42, 52)
BAR_HEIGHTS = (12, 32, 46, 26, 8)
BAR_WIDTH = 5
TILE_RADIUS = 13


def rounded_rect(x, y, x0, y0, x1, y1, radius):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def render(size):
    scale = size * SUPERSAMPLE
    unit = scale / 64.0
    bars = []
    for center, height in zip(BAR_CENTERS, BAR_HEIGHTS):
        x0 = (center - BAR_WIDTH / 2) * unit
        x1 = x0 + BAR_WIDTH * unit
        y0 = (32 - height / 2) * unit
        y1 = y0 + height * unit
        bars.append((x0, y0, x1, y1))
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = px * SUPERSAMPLE + sx
                    y = py * SUPERSAMPLE + sy
                    if not rounded_rect(x, y, 0, 0, scale - 1, scale - 1, TILE_RADIUS * unit):
                        color = (0, 0, 0, 0)  # transparent outside the rounded tile
                    else:
                        color = (*BACKGROUND, 255)
                        for x0, y0, x1, y1 in bars:
                            if rounded_rect(x, y, x0, y0, x1, y1, (x1 - x0) / 2):
                                color = (*BARS, 255)
                                break
                    r += color[0]
                    g += color[1]
                    b += color[2]
                    a += color[3]
            count = SUPERSAMPLE * SUPERSAMPLE
            row.append((r // count, g // count, b // count, a // count))
        rows.append(row)
    return rows


def write_png(path, rows):
    size = len(rows)
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # color type 6 = RGBA
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main():
    big = render(256)
    small = render(64)
    write_png(PACKAGE / "ICON.PNG", small)
    write_png(PACKAGE / "ICON_256.PNG", big)
    write_png(PACKAGE / "app" / "ui" / "images" / "icon_64.png", small)
    write_png(PACKAGE / "app" / "ui" / "images" / "icon_256.png", big)
    print("icons written to", PACKAGE)


if __name__ == "__main__":
    main()
