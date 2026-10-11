"""Generate the FPK icon PNGs from the 音迹 waveform mark (standard library only).

The mark: near-black rounded square with five red waveform bars, matching the
web brand. Rendered at 4x and box-downsampled for smooth edges. Outputs the
sizes fnOS expects: ICON.PNG (64), ICON_256.PNG (256), ui images 64/256.
"""
import struct
import zlib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "fnmusic-journal"
SUPERSAMPLE = 4
BACKGROUND = (14, 14, 17)
BARS = (229, 38, 30)
BAR_HEIGHTS = (58, 112, 160, 112, 58)
BAR_WIDTH = 24
BAR_GAP = 18


def rounded_rect(x, y, x0, y0, x1, y1, radius):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def render(size):
    scale = size * SUPERSAMPLE
    unit = scale / 256.0
    cover = rounded_rect(0, 0, 0, 0, scale - 1, scale - 1, 56 * unit)
    total = len(BAR_HEIGHTS) * BAR_WIDTH + (len(BAR_HEIGHTS) - 1) * BAR_GAP
    left = (256 - total) / 2
    bars = []
    for index, height in enumerate(BAR_HEIGHTS):
        x0 = (left + index * (BAR_WIDTH + BAR_GAP)) * unit
        x1 = x0 + BAR_WIDTH * unit
        y0 = (128 - height / 2) * unit
        y1 = y0 + height * unit
        bars.append((x0, y0, x1, y1))
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            r = g = b = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = px * SUPERSAMPLE + sx
                    y = py * SUPERSAMPLE + sy
                    if not cover:
                        color = (0, 0, 0)
                    else:
                        color = BACKGROUND
                        for x0, y0, x1, y1 in bars:
                            if rounded_rect(x, y, x0, y0, x1, y1, (x1 - x0) / 2):
                                color = BARS
                                break
                    r += color[0]
                    g += color[1]
                    b += color[2]
            count = SUPERSAMPLE * SUPERSAMPLE
            row.append((r // count, g // count, b // count))
        rows.append(row)
    return rows


def write_png(path, rows):
    size = len(rows)
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for r, g, b in row:
            raw += bytes((r, g, b))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
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
