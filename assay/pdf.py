"""Company report as a PDF, written with the standard library.

Times faces with their real metrics, vector charts, the letter's photograph embedded as
JPEG, and every receipt number a live link to its SEC filing.

    python3 -m assay.pdf --data data --ticker INTC --out INTC.pdf [--prices prices.json] [--descriptions d.json] [--history h.json]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import struct
import zlib
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from . import animals
from .grading import grade_label
from .render import FACTORS, FLAG_LABELS, PIOTROSKI_LABELS, PRICE_METRICS, SLEEVES, _company_name, _fifth, _money, fmt, load_peers, long_date, pct_points, peer_factor_medians

PAGE_W, PAGE_H, MARGIN = 612.0, 792.0, 50.0
INNER = PAGE_W - 2 * MARGIN
NAVY = (0.0, 0.0, 0.5)
INK = (0.08, 0.08, 0.1)
GREY = (0.78, 0.78, 0.85)
SHADE = (0.93, 0.93, 0.96)
LINK = (0.0, 0.0, 0.93)

_ROMAN = "250 333 408 500 500 833 778 333 333 333 500 564 250 333 250 278 500 500 500 500 500 500 500 500 500 500 278 278 564 564 564 444 921 722 667 667 722 611 556 722 722 333 389 722 611 889 722 722 556 722 667 556 611 722 722 944 722 722 611 333 278 333 469 500 333 444 500 444 500 444 333 500 500 278 278 500 278 778 500 500 500 500 333 389 278 500 500 722 500 500 444 480 200 480 541"
_BOLD = "250 333 555 500 500 1000 833 333 333 333 500 570 250 333 250 278 500 500 500 500 500 500 500 500 500 500 333 333 570 570 570 500 930 722 667 722 722 667 611 778 778 389 500 778 667 944 722 778 611 778 722 556 667 722 722 1000 722 722 667 333 278 333 581 500 333 500 556 444 556 444 333 500 556 278 333 556 278 833 556 500 556 556 444 389 333 556 500 722 500 500 444 394 220 394 520"
WIDTHS = {False: [int(w) for w in _ROMAN.split()], True: [int(w) for w in _BOLD.split()]}


def text_width(text: str, size: float, bold: bool = False) -> float:
    table = WIDTHS[bold]
    total = 0
    for ch in text:
        code = ord(ch)
        total += table[code - 32] if 32 <= code < 127 else 500
    return total * size / 1000


def wrap(text: str, width: float, size: float, bold: bool = False) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if text_width(trial, size, bold) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return 0, 0


def _pdf_string(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if ch in "()\\":
            out.append("\\" + ch)
        elif 32 <= code < 127:
            out.append(ch)
        elif code == 0x2019:
            out.append("'")
        elif code in (0x201C, 0x201D):
            out.append('"')
        elif code in (0x2013, 0x2014):
            out.append("-")
        elif 160 <= code <= 255:
            out.append(f"\\{code:03o}")
        else:
            out.append("?")
    return "".join(out)


class Pdf:
    """Pages of drawing operators; fonts are the built-in Times faces, images are JPEG streams, links are URI annotations."""

    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self.links: list[list[tuple[float, float, float, float, str]]] = []
        self.images: dict[str, bytes] = {}
        self.new_page()

    def new_page(self) -> None:
        self.pages.append([])
        self.links.append([])

    def _op(self, text: str) -> None:
        self.pages[-1].append(text)

    def text(self, x: float, y: float, s: str, size: float = 9.5, bold: bool = False, italic: bool = False, color: tuple[float, float, float] = INK, align: str = "left") -> None:
        if align == "right":
            x -= text_width(s, size, bold)
        elif align == "center":
            x -= text_width(s, size, bold) / 2
        font = "F3" if italic else ("F2" if bold else "F1")
        r, g, b = color
        self._op(f"BT /{font} {size:.1f} Tf {r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} Td ({_pdf_string(s)}) Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5, color: tuple[float, float, float] = NAVY, dash: bool = False) -> None:
        r, g, b = color
        self._op(f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w {'[2 2] 0 d' if dash else '[] 0 d'} {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def polyline(self, points: list[tuple[float, float]], width: float = 0.8, color: tuple[float, float, float] = NAVY, fill: tuple[float, float, float] | None = None) -> None:
        if len(points) < 2:
            return
        r, g, b = color
        path = " ".join(f"{x:.2f} {y:.2f} {'m' if i == 0 else 'l'}" for i, (x, y) in enumerate(points))
        if fill:
            self._op(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg {path} h f")
        else:
            self._op(f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w [] 0 d {path} S")

    def rect(self, x: float, y: float, w: float, h: float, fill: tuple[float, float, float] | None = None, stroke: tuple[float, float, float] | None = None, width: float = 0.5) -> None:
        ops = []
        if fill:
            ops.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
        if stroke:
            ops.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG {width:.2f} w [] 0 d")
        op = "B" if fill and stroke else ("f" if fill else "S")
        self._op(" ".join(ops) + f" {x:.2f} {y:.2f} {w:.2f} {h:.2f} re {op}")

    def circle(self, cx: float, cy: float, r: float, fill: tuple[float, float, float] | None = NAVY, stroke: tuple[float, float, float] | None = None) -> None:
        k = 0.5523 * r
        path = (f"{cx + r:.2f} {cy:.2f} m {cx + r:.2f} {cy + k:.2f} {cx + k:.2f} {cy + r:.2f} {cx:.2f} {cy + r:.2f} c "
                f"{cx - k:.2f} {cy + r:.2f} {cx - r:.2f} {cy + k:.2f} {cx - r:.2f} {cy:.2f} c {cx - r:.2f} {cy - k:.2f} {cx - k:.2f} {cy - r:.2f} {cx:.2f} {cy - r:.2f} c "
                f"{cx + k:.2f} {cy - r:.2f} {cx + r:.2f} {cy - k:.2f} {cx + r:.2f} {cy:.2f} c")
        ops = []
        if fill:
            ops.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
        if stroke:
            ops.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG 0.8 w")
        self._op(" ".join(ops) + " " + path + (" B" if fill and stroke else (" f" if fill else " S")))

    def image(self, name: str, data: bytes, x: float, y: float, w: float, h: float) -> None:
        self.images[name] = data
        self._op(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /{name} Do Q")

    def link(self, x: float, y: float, w: float, h: float, url: str) -> None:
        self.links[-1].append((x, y, x + w, y + h, url))

    def build(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_ids = [add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode()) for name in ("Times-Roman", "Times-Bold", "Times-Italic")]
        image_ids = {}
        for name, data in self.images.items():
            w, h = _jpeg_size(data)
            image_ids[name] = add(f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream")
        content_ids = []
        for content in self.pages:
            stream = zlib.compress("\n".join(content).encode("latin-1"), 9)
            content_ids.append(add(f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream"))
        annots_ids = []
        for page_links in self.links:
            annots_ids.append([add(f"<< /Type /Annot /Subtype /Link /Rect [{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f}] /Border [0 0 0] /A << /S /URI /URI ({_pdf_string(url)}) >> >>".encode()) for x1, y1, x2, y2, url in page_links])
        fonts = " ".join(f"/F{i + 1} {fid} 0 R" for i, fid in enumerate(font_ids))
        xobjects = " ".join(f"/{name} {oid} 0 R" for name, oid in image_ids.items())
        pages_id = len(objects) + len(self.pages) + 1
        page_ids = []
        for i, cid in enumerate(content_ids):
            annots = f" /Annots [{' '.join(f'{a} 0 R' for a in annots_ids[i])}]" if annots_ids[i] else ""
            page_ids.append(add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] /Resources << /Font << {fonts} >> /XObject << {xobjects} >> >> /Contents {cid} 0 R{annots} >>".encode()))
        assert add(f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(page_ids)} >>".encode()) == pages_id
        catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
        out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)


class Report:
    """Cursor layout on Letter pages: section headings, wrapped paragraphs, ruled tables, running footer."""

    def __init__(self, title: str, as_of: str) -> None:
        self.pdf = Pdf()
        self.title, self.as_of = title, as_of
        self.y = PAGE_H - MARGIN
        self.page_no = 1

    def footer(self) -> None:
        self.pdf.line(MARGIN, MARGIN - 12, PAGE_W - MARGIN, MARGIN - 12, 0.4, GREY)
        self.pdf.text(MARGIN, MARGIN - 24, "Not investment advice. Informational and educational only. Data from SEC EDGAR, may contain errors, is not warranted. The grade ranks reported financial condition and is not a return forecast.", 6.5, color=INK)
        self.pdf.text(PAGE_W - MARGIN, MARGIN - 34, f"{self.title}, as of {long_date(self.as_of)}, page {self.page_no}", 6.5, align="right")

    def need(self, height: float) -> None:
        if self.y - height < MARGIN + 6:
            self.footer()
            self.pdf.new_page()
            self.page_no += 1
            self.y = PAGE_H - MARGIN

    def heading(self, text: str, note: str | None = None) -> None:
        self.need(34)
        self.y -= 12
        self.pdf.text(MARGIN, self.y, text, 11, bold=True, color=NAVY)
        if note:
            self.pdf.text(PAGE_W - MARGIN, self.y, note, 7.5, italic=True, align="right")
        self.y -= 4
        self.pdf.line(MARGIN, self.y, PAGE_W - MARGIN, self.y, 0.7)
        self.y -= 13

    def paragraph(self, text: str, size: float = 9.5, italic: bool = False, color: tuple[float, float, float] = INK, width: float = INNER) -> None:
        for line in wrap(text, width, size):
            self.need(size + 4)
            self.pdf.text(MARGIN, self.y, line, size, italic=italic, color=color)
            self.y -= size + 3.5
        self.y -= 2

    def table(self, columns: list[tuple[str, float, str]], rows: list[Any], size: float = 8.5, shade_groups: bool = True, links: dict[int, str] | None = None) -> None:
        """columns: (header, width, align in left|right|wrap). A row is a list of cells, a string for a group line, or a tuple (cells, draw) with a callback."""
        x0 = MARGIN
        total = sum(w for _, w, _ in columns)
        self.need(size + 16)
        x = x0
        for header, width, align in columns:
            self.pdf.text(x + (width - 3 if align == "right" else 2), self.y, header, size - 1, bold=True, color=NAVY, align=("right" if align == "right" else "left"))
            x += width
        self.y -= 4
        self.pdf.line(x0, self.y, x0 + total, self.y, 0.7)
        self.y -= size + 3
        for n, row in enumerate(rows):
            draw = None
            if isinstance(row, tuple):
                row, draw = row
            if isinstance(row, str):
                self.need(size + 8)
                self.pdf.rect(x0, self.y - 3.5, total, size + 6, fill=SHADE)
                self.pdf.text(x0 + 2, self.y, row, size - 0.5, bold=True, color=NAVY)
                self.y -= size + 6
                continue
            cells = [("" if c is None else str(c)) for c in row]
            wrapped = [wrap(c, w - 5, size) if a == "wrap" else [c] for c, (_, w, a) in zip(cells, columns)]
            lines = max(len(w) for w in wrapped) if wrapped else 1
            height = size * lines + 4 * (lines - 1) + 4
            self.need(height)
            x = x0
            for (header, width, align), text_lines in zip(columns, wrapped):
                for i, line in enumerate(text_lines):
                    self.pdf.text(x + (width - 3 if align == "right" else 2), self.y - i * (size + 4), line, size, align=("right" if align == "right" else "left"))
                x += width
            if draw:
                draw(self.pdf, x0, self.y, columns)
            if links and n in links:
                self.pdf.link(x0, self.y - 3, total, size + 4, links[n])
            self.y -= height
            self.pdf.line(x0, self.y + height - 3.4, x0 + total, self.y + height - 3.4, 0.3, GREY)
        self.y -= 6


def _bar(pdf: Pdf, x: float, y: float, width: float, percentile: float | None) -> None:
    pdf.line(x, y, x + width, y, 0.5, GREY)
    for f in (0.2, 0.4, 0.6, 0.8):
        pdf.line(x + width * f, y - 2, x + width * f, y + 2, 0.5, GREY)
    if percentile is not None:
        pdf.rect(x + width * percentile - 1, y - 3.5, 2, 7, fill=NAVY)


def _ruler(pdf: Pdf, x: float, y: float, width: float, percentile: float) -> None:
    pdf.line(x, y, x + width, y, 0.6)
    for f in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        pdf.line(x + width * f, y - 3, x + width * f, y + 3, 0.6)
    for i, g in enumerate("ABCDE"):
        pdf.text(x + width * (0.1 + 0.2 * i), y - 11, g, 7, align="center", color=NAVY)
    px = x + width * percentile
    pdf.polyline([(px - 3.5, y + 11), (px + 3.5, y + 11), (px, y + 4)], fill=NAVY)


def _price_chart(pdf: Pdf, x0: float, y0: float, w: float, h: float, prices: list[list[Any]], history: list[dict[str, Any]]) -> None:
    closes = [float(p[1]) for p in prices]
    days = [p[0] for p in prices]
    lo, hi = min(closes), max(closes)
    pad = (hi - lo) * 0.08 or 1
    span = hi - lo + 2 * pad
    left, right, top, bottom = 30, 34, 14, 16
    pw, ph = w - left - right, h - top - bottom

    def xy(i: int, c: float) -> tuple[float, float]:
        return (x0 + left + i / (len(closes) - 1) * pw, y0 + bottom + (c - lo + pad) / span * ph)

    pts = [xy(i, c) for i, c in enumerate(closes)]
    step = 10 ** math.floor(math.log10(span / 2.5))
    for m in (1, 2, 5, 10):
        if m * step >= span / 3.2:
            step *= m
            break
    v = math.ceil((lo - pad) / step) * step
    while v < hi + pad:
        yy = y0 + bottom + (v - lo + pad) / span * ph
        pdf.line(x0 + left, yy, x0 + left + pw, yy, 0.3, GREY)
        pdf.text(x0 + left - 3, yy - 2.5, f"${v:,.0f}", 6.5, align="right")
        v += step
    pdf.line(x0 + left, y0 + bottom, x0 + left + pw, y0 + bottom, 0.6)
    pdf.polyline([(pts[0][0], y0 + bottom), *pts, (pts[-1][0], y0 + bottom)], fill=(0.93, 0.93, 0.97))
    pdf.polyline(pts, 0.9)
    seen: set[str] = set()
    every = 1 if pw > 320 else 2
    for i, d in enumerate(days):
        if d[:7] not in seen and d[8:10] <= "03" and 0 < i < len(days) - 5:
            seen.add(d[:7])
            if len(seen) % every == 1 or every == 1:
                pdf.text(pts[i][0], y0 + 4, date.fromisoformat(d).strftime("%b"), 6.5, align="center")
    for point in sorted((p for p in (history or []) if p.get("grade")), key=lambda p: p["date"]):
        if point["date"] < days[0]:
            idx = 0
        else:
            idx = next((i for i, d in enumerate(days) if d >= point["date"]), len(days) - 1)
        x, y = pts[idx]
        pdf.circle(x, y, 2.4)
        pdf.line(x, y + 4, x, y + 11, 0.6)
        pdf.text(x, y + 13, point["grade"], 7.5, bold=True, align="center", color=NAVY)
    pdf.circle(pts[-1][0], pts[-1][1], 2.4, fill=(1, 1, 1), stroke=NAVY)
    pdf.text(pts[-1][0] + 5, pts[-1][1] - 2.5, f"${closes[-1]:,.2f}", 7, bold=True)


def _density(pdf: Pdf, x0: float, y0: float, w: float, h: float, values: list[float], focal: float | None, label: str) -> None:
    xs = sorted(values)
    n = len(xs)
    sd = statistics.pstdev(xs) or 1e-6
    bw = 1.06 * sd * n ** (-0.2)
    lo, hi = xs[0] - 2 * bw, xs[-1] + 2 * bw
    span = hi - lo

    def density(v: float) -> float:
        return sum(math.exp(-0.5 * ((v - x) / bw) ** 2) for x in xs) / (n * bw * math.sqrt(2 * math.pi))

    grid = [lo + span * i / 120 for i in range(121)]
    dens = [density(v) for v in grid]
    peak = max(dens) or 1
    top, bottom = 16, 14
    pts = [(x0 + (v - lo) / span * w, y0 + bottom + d / peak * (h - top - bottom)) for v, d in zip(grid, dens)]
    edges = [xs[min(n - 1, int(round(q * (n - 1))))] for q in (0.2, 0.4, 0.6, 0.8)]
    bounds = [lo, *edges, hi]
    for i, g in enumerate("ABCDE"):
        pdf.text(x0 + ((bounds[i] + bounds[i + 1]) / 2 - lo) / span * w, y0 + 3, g, 8, bold=True, align="center", color=NAVY)
    for e in edges:
        ex = x0 + (e - lo) / span * w
        pdf.line(ex, y0 + bottom, ex, y0 + h - 4, 0.4, GREY)
    pdf.polyline([(pts[0][0], y0 + bottom), *pts, (pts[-1][0], y0 + bottom)], fill=(0.93, 0.93, 0.97))
    pdf.polyline(pts, 0.9)
    pdf.line(x0, y0 + bottom, x0 + w, y0 + bottom, 0.6)
    if focal is not None:
        fx, fy = x0 + (focal - lo) / span * w, y0 + bottom + density(focal) / peak * (h - top - bottom)
        pdf.line(fx, fy, fx, y0 + bottom, 0.6, dash=True)
        pdf.circle(fx, fy, 3)
        pdf.text(fx + (6 if fx < x0 + w * 0.6 else -6), fy + 5, label, 8, bold=True, align=("left" if fx < x0 + w * 0.6 else "right"), color=NAVY)


def _rescan_chart(pdf: Pdf, x0: float, y0: float, w: float, h: float, rescans: list[tuple[str, str | None, float | None]]) -> None:
    """Percentile at every rescan as a step line over the five letter bands, years along the baseline."""
    left, right, top, bottom = 22, 8, 6, 14
    pw, ph = w - left - right, h - top - bottom
    n = len(rescans)

    def x_at(i: int) -> float:
        return x0 + left + i / max(n - 1, 1) * pw

    def y_at(p: float) -> float:
        return y0 + bottom + (1 - p) * ph

    for q, g in zip((0.2, 0.4, 0.6, 0.8, 1.0), "ABCDE"):
        if q < 1:
            pdf.line(x0 + left, y_at(q), x0 + left + pw, y_at(q), 0.3, GREY)
        pdf.text(x0 + left - 4, y_at(q - 0.1) - 2.5, g, 7, bold=True, align="right")
    pdf.line(x0 + left, y_at(0), x0 + left + pw, y_at(0), 0.5, NAVY)
    segment: list[tuple[float, float]] = []
    for i, (_, _, pct) in enumerate(rescans):
        if pct is None:
            if len(segment) > 1:
                pdf.polyline(segment, 0.9, NAVY)
            segment = []
        else:
            segment.append((x_at(i), y_at(pct)))
    if len(segment) > 1:
        pdf.polyline(segment, 0.9, NAVY)
    elif len(segment) == 1:
        pdf.circle(segment[0][0], segment[0][1], 1.2)
    seen: set[str] = set()
    for i, (day, _, _) in enumerate(rescans):
        if day[:4] not in seen:
            seen.add(day[:4])
            if i > 0:
                pdf.text(x_at(i), y0 + 3, day[:4], 6.5, align="center")


def company_report(report: dict[str, Any], prices: list[list[Any]] | None = None, description: dict[str, Any] | None = None, history: list[dict[str, Any]] | None = None,
                   peers: list[dict[str, Any]] | None = None, medians: dict[str, float] | None = None, rescans: list[tuple[str, str | None, float | None]] | None = None) -> bytes:
    company, grade = report["company"], report["grade"]
    ticker, as_of = company["ticker"], report["as_of"]
    name = _company_name(company["name"])
    sector_label = (company.get("sector") or "").replace("_", " ")
    doc = Report(f"Assay {ticker}", as_of)
    pdf = doc.pdf
    sd = grade.get("sampling_standard_deviation")
    graded = bool(grade.get("status") == "resolved" and grade.get("grade"))
    peers = peers or []
    medians = medians or {}
    condition = report.get("condition") or {}
    stratum, rel = condition.get("stratum") or {}, ((condition.get("reliability") or {}).get("stratum")) or {}
    snapshot, live = report.get("market_snapshot") or {}, report.get("live_implied_expectations") or {}
    diagnostics = report.get("diagnostics") or {}
    gap = diagnostics.get("filing_gap") or {}
    flags = report.get("flags", [])
    active = [f for f in flags if f.get("active")]
    latest_filed = max((f.get("filed") or "" for f in report.get("facts", [])), default=None)

    # ---- masthead
    pdf.text(MARGIN, doc.y - 6, "ASSAY", 15, bold=True, color=NAVY)
    pdf.text(MARGIN + 66, doc.y - 6, "Financial-condition grades for SEC filers", 8.5, color=NAVY)
    pdf.text(PAGE_W - MARGIN, doc.y - 6, f"Company Report, as of {long_date(as_of)}", 8.5, align="right", color=NAVY)
    doc.y -= 13
    pdf.line(MARGIN, doc.y, PAGE_W - MARGIN, doc.y, 0.8)
    doc.y -= 24
    pdf.text(MARGIN, doc.y, f"{name} ({ticker})", 17, bold=True)
    doc.y -= 13
    pdf.text(MARGIN, doc.y, f"{company.get('exchange') or ''}; {sector_label}; SIC {company.get('sic')}, {company.get('sic_description') or ''}; CIK {int(company['cik']):010d}", 8.5)
    doc.y -= 14

    # ---- scoreboard panel: left column of rows, right column with portrait and chart
    left_w, right_w = 296.0, INNER - 296.0
    panel_top = doc.y
    rows: list[tuple[str, str, str | None, Any]] = []
    if graded:
        letter = grade["grade"]
        rank = 1 + sum(1 for p in peers if (p.get("percentile") or 0) < grade["percentile"])
        rows.append(("Grade", "", None, "grade"))
        rows.append(("Rank", f"{rank} of {len(peers)}" if peers else f"percentile {pct_points(grade['percentile'])}", None, None))
        for key, label in SLEEVES:
            value = (grade.get("sleeve_scores") or {}).get(key)
            rows.append((label, grade_label(value, sd or 0) if value is not None else "n/a", f"percentile {pct_points(value)}", ("bar", value)))
    else:
        rows.append(("Grade", "None", f"{grade.get('coverage', {}).get('resolved', 0)} of 14 inputs computable; 7 are required", None))
    if rel:
        rows.append(("Reliability", f"{rel['auc']:.2f} AUC", f"filers with {stratum.get('label') or ''}; 95% interval {rel['ci'][0]:.2f} to {rel['ci'][1]:.2f}; {rel['test_events']} events, 2020 to 2025", None))
        rows.append(("Peer failure rate", f"{rel['base_rate_annual'] * 100:.2f}% a year", "Item 1.03 bankruptcies, same asset band, 2012 to 2025", None))
    if stratum:
        rows.append(("Size band", stratum.get("label") or "", f"total assets {_money(stratum.get('assets') or 0)}", None))
    universe = condition.get("standard_universe") or {}
    if universe.get("inside") is not None:
        rows.append(("Standard universe", "inside" if universe["inside"] else "outside", "revenue above $1M and assets above $10M", None))
    warn = ", ".join(FLAG_LABELS.get(f["name"], f["name"]).lower() for f in active if f["name"] != "fortress") or "none active"
    if any(f["name"] == "fortress" for f in active):
        warn += "; fortress balance sheet"
    unresolved = [FLAG_LABELS.get(f["name"], f["name"]).lower() for f in flags if f.get("status") == "unresolved"]
    rows.append(("Warnings", warn, ("not computable: " + ", ".join(unresolved)) if unresolved else None, None))
    rows.append(("Inputs computable", f"{grade.get('coverage', {}).get('resolved', 0)} of 14", None, None))
    rows.append(("Graded from", f"filings received through {long_date(latest_filed)}", f"{(condition.get('data_age') or {}).get('days')} days before the rescan; latest periodic filing {gap.get('last_form') or ''} received {long_date(gap.get('last_filing_date'))}", None))
    if snapshot.get("price") is not None:
        rows.append(("Last close", f"${snapshot['price']:,.2f}", f"{str(snapshot.get('price_timestamp') or '')[:10]}; market equity {_money(live.get('market_equity') or 0)}", None))
    rows.append(("Price used in grade", "None", None, None))

    # measure
    heights = []
    for label, value, sub, extra in rows:
        if extra == "grade":
            heights.append(62)
        elif sub:
            heights.append(14 + 10 * len(wrap(sub, left_w - 112, 7.5)))
        else:
            heights.append(17)
    header_h = 26
    panel_h = header_h + sum(heights) + 2
    doc.need(panel_h + 4)
    panel_top = doc.y
    x0 = MARGIN
    pdf.rect(x0, panel_top - panel_h, INNER, panel_h, stroke=NAVY, width=0.8)
    pdf.rect(x0, panel_top - header_h, INNER, header_h, fill=SHADE, stroke=NAVY, width=0.8)
    pdf.text(x0 + 6, panel_top - 11, name, 10, bold=True, color=NAVY)
    pdf.text(x0 + 6, panel_top - 21, f"{ticker}; {sector_label}", 8, color=NAVY)
    pdf.line(x0 + left_w, panel_top - header_h, x0 + left_w, panel_top - panel_h, 0.8)
    y = panel_top - header_h
    for (label, value, sub, extra), h in zip(rows, heights):
        pdf.line(x0, y, x0 + left_w, y, 0.4, GREY)
        pdf.text(x0 + 6, y - 12, label, 8.5, bold=True, color=NAVY)
        vx = x0 + 104
        if extra == "grade":
            pdf.text(vx, y - 30, grade["grade"], 30, bold=True, color=NAVY)
            _ruler(pdf, vx + 62, y - 26, 120, grade["percentile"])
            pdf.text(vx, y - 44, f"{_fifth(grade['grade'])} of {grade['peer_count']} graded peers", 7.5)
            pdf.text(vx, y - 54, f"percentile {pct_points(grade['percentile'])}; sampling error {pct_points(sd)} points; lower is stronger", 7.5)
        elif isinstance(extra, tuple) and extra[0] == "bar":
            pdf.text(vx, y - 12, value, 9)
            _bar(pdf, vx + 30, y - 9, 90, extra[1])
            pdf.text(vx + 128, y - 12, sub or "", 7.5)
        else:
            pdf.text(vx, y - 12, value, 9)
            if sub:
                for i, line in enumerate(wrap(sub, left_w - 112, 7.5)):
                    pdf.text(vx, y - 22 - 10 * i, line, 7.5)
        y -= h
    # right column: portrait and chart
    rx = x0 + left_w + 8
    ry = panel_top - header_h - 8
    rw = right_w - 16
    if graded:
        photo = resources.files("assay").joinpath("assets").joinpath(f"{animals.LETTERS.get(grade['grade'][0], ('', ''))[0]}-240.jpg")
        if photo.is_file():
            pdf.image("Portrait", photo.read_bytes(), rx, ry - 68, 92, 68)
            pdf.rect(rx, ry - 68, 92, 68, stroke=NAVY)
            animal_label = animals.LETTERS.get(grade["grade"][0], ("", ""))[1]
            pdf.text(rx + 100, ry - 14, f"{grade['grade']}, the {animal_label.lower()}", 9, bold=True, color=NAVY)
            for i, line in enumerate(wrap(f"{_fifth(grade['grade'])} of its sector; letters are fifths of the {grade['peer_count']} graded peers", rw - 104, 7.5)):
                pdf.text(rx + 100, ry - 26 - 10 * i, line, 7.5)
        ry -= 80
    if prices and len(prices) >= 20:
        chart_h = min(150.0, ry - (panel_top - panel_h) - 30)
        pdf.text(rx, ry - 9, "Share price, twelve months to the rescan", 8, bold=True, color=NAVY)
        pdf.text(rx, ry - 19, "split-adjusted close; the letter is fixed at the rescan that set it", 6.5, italic=True)
        _price_chart(pdf, rx, ry - 24 - chart_h, rw, chart_h, prices, history or [])
        ry -= 30 + chart_h
    points = [p for p in (history or []) if p.get("grade")]
    if points and ry - 12 > panel_top - panel_h + 8:
        pdf.text(rx, ry - 9, "Rescans on file: " + ", ".join(f"{p['grade']} from {long_date(p['date'])}" for p in points[-4:]), 6.5, italic=True)
    doc.y = panel_top - panel_h - 12

    # ---- every rescan on file
    if rescans and len(rescans) >= 3:
        doc.heading("Letter at every rescan", f"{long_date(rescans[0][0])} to {long_date(rescans[-1][0])}; sector percentile, stronger toward the top; facts filed by each date only")
        doc.need(90)
        _rescan_chart(pdf, MARGIN, doc.y - 84, INNER, 80, rescans)
        doc.y -= 92

    # ---- description
    if description and description.get("text"):
        doc.heading("Business, in the company's words", f"opening of Item 1 of the {description.get('form') or '10-K'} received {long_date(description.get('filed'))}")
        doc.paragraph(description["text"], 9.5)

    # ---- inputs
    doc.heading("The fourteen inputs", "lower percentile is stronger; equal-weighted sleeves")
    percentiles = grade.get("factor_percentiles") or {}
    factors = {f["name"]: f for f in report["factors"]}
    rows_t: list[Any] = []
    for key, label in SLEEVES:
        value = (grade.get("sleeve_scores") or {}).get(key)
        rows_t.append(f"{label}, sleeve percentile {pct_points(value)}" if value is not None else label)
        for fname, (flabel, kind, _) in FACTORS.items():
            factor = factors.get(fname)
            if factor is None or factor.get("sleeve") != key:
                continue
            pc = percentiles.get(fname) or {}
            median = fmt(medians.get(fname), kind) if fname in medians else "n/a"
            if factor.get("value") is not None:
                pval = pc.get("percentile")

                def draw(p: Pdf, x: float, yy: float, cols: list, pval=pval) -> None:
                    _bar(p, x + cols[0][1] + 4, yy + 3, cols[1][1] - 8, pval)

                rows_t.append(([flabel, "", fmt(factor["value"], kind), median, pct_points(pval), pc.get("peer_count") or "", factor.get("period_end") or ""], draw))
            else:
                rows_t.append([flabel, "", "not computable", median, "", "", (factor.get("detail") or factor.get("reason") or "")])
    doc.table([("Input", 156, "left"), ("Percentile, 0 to 100", 84, "left"), ("Value", 62, "right"), ("Peer median", 66, "right"), ("Percentile", 50, "right"), ("Peers", 40, "right"), ("Period end", 54, "wrap")], rows_t, 8)

    # ---- warnings: only the checks that triggered
    triggered = [f for f in flags if f.get("active")]
    if triggered:
        doc.heading("Warnings", f"{len(triggered)} of {len(flags)} checks triggered; none of them enters the letter")
        doc.table([("Check", 110, "left"), ("Measured", 402, "wrap")], [[FLAG_LABELS.get(f["name"], f["name"]), f.get("detail") or ""] for f in triggered], 8)

    # ---- peers
    if peers and graded:
        doc.heading(f"Among {len(peers)} peers", f"{sector_label}; letters are fifths of this list")
        scored = [p["composite"] for p in peers if isinstance(p.get("composite"), (int, float))]
        if len(scored) >= 10:
            doc.need(120)
            _density(pdf, MARGIN, doc.y - 108, INNER, 104, scored, grade.get("composite"), f"{ticker} {pct_points(grade['percentile'])}")
            doc.y -= 116
            doc.paragraph("Composite score of every graded peer, smoothed; vertical rules are the letter band edges; stronger to the left.", 7.5, italic=True)
        ordered = sorted(peers, key=lambda p: p.get("percentile") or 0)
        pos = next((i for i, p in enumerate(ordered) if p["ticker"] == ticker), None)
        if pos is not None:
            window = ordered[max(0, pos - 5): pos + 6]
            doc.table([("Rank", 40, "right"), ("Company", 300, "wrap"), ("Percentile", 70, "right"), ("Letter", 50, "right"), ("Warnings", 52, "wrap")], [
                [ordered.index(p) + 1, f"{_company_name(p.get('name') or '')} ({p['ticker']})" + (" <" if p["ticker"] == ticker else ""), pct_points(p.get("percentile")), p.get("grade") or "", ", ".join(FLAG_LABELS.get(f, f).lower() for f in (p.get("active_flags") or []))]
                for p in window], 8)

    # ---- price measures
    metrics = {m["name"]: m for m in (report.get("implied_expectations") or {}).get("metrics", [])}
    rows_p: list[Any] = []
    for mname, (mlabel, kind, _) in PRICE_METRICS.items():
        metric = metrics.get(mname)
        if not metric:
            continue
        value = metric.get("value")
        peer_values = [p["implied_expectations"][mname] for p in peers if isinstance((p.get("implied_expectations") or {}).get(mname), (int, float))]
        share = (sum(1 for v in peer_values if v < value) / len(peer_values) * 100) if (peer_values and value is not None) else None
        rows_p.append([mlabel, fmt(value, kind) if value is not None else "not computable", fmt(statistics.median(peer_values), kind) if peer_values else "n/a", f"{share:.0f}" if share is not None else ""])
    if rows_p:
        doc.heading("What the price implies", f"at the {str(snapshot.get('price_timestamp') or as_of)[:10]} close; none enters the grade")
        doc.table([("Measure", 262, "left"), ("Value", 80, "right"), ("Peer median", 90, "right"), ("Percentile", 80, "right")], rows_p, 8)

    # ---- diagnostics
    piot = diagnostics.get("piotroski_f_score") or {}
    runway = diagnostics.get("cash_runway") or {}
    adv = diagnostics.get("median_dollar_adv") or {}
    doc.heading("Diagnostics", "not in the grade")
    rows_d: list[Any] = []
    if piot.get("components"):
        rows_d.append(f"Piotroski F-score {piot.get('score')} of {piot.get('maximum') or 9}, period end {piot.get('period_end') or ''}")
        rows_d += [[PIOTROSKI_LABELS.get(c.get("name"), str(c.get("name"))), "pass" if c.get("passed") else "fail"] for c in piot["components"]]
    rows_d.append("Cash, filings, liquidity")
    cash_input = runway.get("cash_input") if isinstance(runway.get("cash_input"), dict) else None
    rows_d += [
        ["Cash and equivalents", _money(cash_input["value"]) if cash_input and isinstance(cash_input.get("value"), (int, float)) else "n/a"],
        ["Operating cash flow, four quarters", _money(runway["trailing_operating_cash_flow"]) if isinstance(runway.get("trailing_operating_cash_flow"), (int, float)) else "n/a"],
        ["Months of runway", f"{runway['months']:.1f}" if isinstance(runway.get("months"), (int, float)) else "not finite"],
        ["Days since last periodic filing", f"{gap.get('days')} ({gap.get('last_form') or ''} received {long_date(gap.get('last_filing_date'))})" if gap.get("days") is not None else "n/a"],
        ["Median daily dollar volume, 63 sessions", _money(adv["value"]) if isinstance(adv.get("value"), (int, float)) else "n/a"],
    ]
    doc.table([("Measure", 330, "left"), ("Value", 182, "right")], rows_d, 8)

    # ---- sources
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for factor in report["factors"]:
        for r in factor.get("inputs") or []:
            key = (str(r.get("accession")), str(r.get("tag")), str(r.get("end")))
            if r.get("filing_url") and key not in seen:
                seen[key] = r
    receipts = list(seen.values())
    doc.heading("Sources", "each row links to the filing index on sec.gov")
    rows_s = []
    links: dict[int, str] = {}
    for n, r in enumerate(receipts):
        value = r.get("value")
        shown = _money(value) if r.get("unit") == "USD" and isinstance(value, (int, float)) else (f"{value:,.0f}" if isinstance(value, (int, float)) else str(value))
        rows_s.append([n + 1, str(r.get("concept") or "").replace("_", " "), f"{r.get('namespace')}:{r.get('tag')}", shown, r.get("end"), r.get("filed"), r.get("form"), r.get("accession")])
        links[n] = r["filing_url"]
    doc.table([("#", 20, "right"), ("Concept", 82, "left"), ("XBRL tag", 176, "wrap"), ("Value", 56, "right"), ("Period end", 50, "left"), ("Received", 50, "left"), ("Form", 30, "left"), ("Accession", 48, "wrap")], rows_s, 6.8, links=links)
    doc.footer()
    return pdf.build()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="write a company report PDF from a ticker artifact")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--descriptions", type=Path)
    parser.add_argument("--history", type=Path)
    args = parser.parse_args(argv)
    ticker = args.ticker.upper()
    report = json.loads((args.data / "tickers" / f"{ticker}.json").read_text(encoding="utf-8"))
    prices = json.loads(args.prices.read_text(encoding="utf-8")).get(ticker) if args.prices else None
    description = json.loads(args.descriptions.read_text(encoding="utf-8")).get(ticker) if args.descriptions else None
    history = report.get("grade_history")
    if args.history:
        from .history import ticker_history
        history = ticker_history(json.loads(args.history.read_text(encoding="utf-8")), ticker)
    peers = load_peers(args.data, report["grade"].get("peer_group") or report["company"].get("sector") or "")
    medians, _ = peer_factor_medians(args.data, peers) if peers else ({}, {})
    args.out.write_bytes(company_report(report, prices, description, history, peers, medians))
    print(args.out, args.out.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
