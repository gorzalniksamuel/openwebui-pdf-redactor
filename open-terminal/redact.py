#!/usr/bin/env python3
"""redact - lean, fast PDF redaction for Open Terminal (no OCR).

    redact extract FILE.pdf [--json]
    redact apply   FILE.pdf --terms "Max Mustermann" [--terms ...] [--regex PAT ...]
                            [--builtin IBAN ...] [--mode flatten|text] [-o OUT.pdf] [--json]
    redact verify  OUT.pdf  --terms ... [--regex ...] [--builtin ...] [--mode flatten|text]
                            [--expect-pages N] [--json]
    redact builtins

Pipeline of `apply`:
  1. find every occurrence (case-insensitive, word boundaries) on the text layer
  2. PyMuPDF true redaction, two passes:
       a) full padded rects: image pixels blanked, touching line art removed, black fill
       b) vertically inset rects: text glyphs removed (keeps glyphs of adjacent lines)
  3. flatten (default): Ghostscript renders every page at 200 dpi -> new image-only PDF
     text: keep the remaining text layer
  4. Ghostscript pdfwrite rewrite
  5. PyMuPDF scrub (metadata, XMP, JS, embedded files, annots, links, outlines, thumbnails,
     hidden text, forms) + non-incremental save, garbage=4

Text inside images is read with Tesseract: small images in several variants (faint/rotated/mirrored text),
full-page images (scans, already redacted/flattened PDFs) once per page in document mode. Pages without a text
layer are therefore redactable too (OCR quality decides; the output is verified by OCR as well).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf

FULL_APP = os.environ.get("REDACT_FULL_APP_URL", "")  # optional link to a full redaction app, shown in warnings
FLATTEN_DPI = 200
TEXT_BAND_INSET = 0.25  # fraction trimmed on both sides PERPENDICULAR to the line direction for the text pass
IMAGE_HEAVY = 0.5  # pages whose images cover >= this share of the page: text inside images is not checked
SIDECAR_EXT = ".redact.json"  # next to the output: redaction rects (coordinates only, no text) for verify
VERIFIED_EXT = ".verified"  # written by a passing verify: sha256 of the PDF; the Open WebUI delivery tool requires it
TARGETS_EXT = ".targets.json"  # next to the INPUT: the targets of the last apply (for --more); swept like the PDFs
PAD_X, PAD_Y = 1.0, 0.5
GS_TIMEOUT = 300
OCR_LANGS = "deu+eng"
OCR_DPI = 300
OCR_MIN_CONF = 30
OCR_MAX_IMAGES = 40  # per document; images beyond that are reported, not OCRed
BIG_IMAGE = 0.5  # an image covering this share of the page is a scan/page image: document-mode OCR, word-level only
OCR_TIMEOUT = 60
# (angle, threshold, mirrored), most productive first; a region stops at its first variant that reveals a target.
# A mirrored copy (ticket "Zangenabdruck") sits in the same image as the upright one, which removes the image.
SCAN_VARIANTS = ((0, None, False), (0, 230, False), (10, 235, False), (-10, None, False), (0, 200, False),
                 (0, None, True))
SCAN_DPI = 300
SCAN_WORKERS = max(2, min(4, os.cpu_count() or 2))
EXTRACT_MAX_CHARS = 9000  # extract output budget (the 9b model has a 16k-token context incl. instructions)

# Same patterns as the full app's Builtins.cs (Python re syntax).
_MONTHS = (r"Jan(?:uary|uar)?|Feb(?:ruary|ruar)?|M(?:a|ä)r(?:ch|z)?|Apr(?:il)?|Mai|May|Jun[ie]?|Jul[iy]?"
           r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Okt(?:ober)?|Oct(?:ober)?|Nov(?:ember)?|Dez(?:ember)?|Dec(?:ember)?")
BUILTINS = {
    "EMAIL": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}",
    "PHONE": (r"(?<![\w+(])(?:\+\d{1,3}[ \n\-]?\(?\d{1,5}\)?(?:[ \n\-/]?\d{2,}){1,4}"
              r"|\(\d{2,4}\)[ \n\-]?\d{3}[ \n\-]?\d{3,4}|0\d{2,5}[ \n\-/]\d{5,}(?:[ \n\-]\d{2,})?"
              r"|0\d{9,12}|\d{3}-\d{3}-\d{4})(?!\d)"),
    "IBAN": r"\b[A-Z]{2}\d{2}(?:[ \n]?[A-Z0-9]{4}){2,7}(?:[ \n]?[A-Z0-9]{1,3})?\b",
    "DATE": (r"(?<![\d.])\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})(?!\d)"
             r"|(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"
             r"|(?<!\d)\d{1,2}/\d{1,2}/\d{4}(?!\d)"
             r"|\b(?i:" + _MONTHS + r")\.?[ \n]\d{1,2}(?:st|nd|rd|th)?,?[ \n]\d{4}\b"
             r"|\b\d{1,2}\.?[ \n](?i:" + _MONTHS + r")\.?[ \n]\d{4}\b"),
    "NUMBER": r"\d+(?:[.,'’\-/]\d+)*",
    "URL": r"\b(?:https?://|www\.)[^\s<>\"']*[^\s<>\"'.,;:!?)\]]",
    "POSTCODE_DE": (r"\bDE?-(?:0[1-9]\d{3}|[1-9]\d{4})(?!\d|[.,\-/]\d)"
                    r"|(?<![\d.,\-/])(?:0[1-9]\d{3}|[1-9]\d{4})(?!\d|[.,\-/]\d)"),
    "CREDIT_CARD": r"(?<![\d\-])(?:\d{4}([ \-]?)\d{4}\1\d{4}\1\d{4}|\d{4}([ \-]?)\d{6}\2\d{5})(?!\d)",
    "IP_ADDRESS": (r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\d|\.\d)"
                   r"|(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])"
                   r"|(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?(?![\w:])"
                   r"|(?<![\w:]):(?::[0-9A-Fa-f]{1,4}){1,7}(?![\w:])"),
    "TAX_ID_DE": r"(?<![\d/])\d{2} ?\d{3} ?\d{3} ?\d{3}(?!\d)|(?<![\d/])\d{2,3}/\d{3,4}/\d{4,5}(?!\d)|\bDE ?\d{9}\b",
    # German statutory health insurance number (Krankenversichertennummer): letter + 9 digits
    "HEALTH_INSURANCE_DE": r"(?<![\w])[A-Z] ?\d{9}(?![\w])",
    # ICD-10 diagnosis codes (I21.4, E11.9 G, Z00.0)
    "ICD10": r"(?<![\w.])[A-TV-Z]\d[0-9A-Z](?:\.[0-9A-Z]{1,4})?(?: ?[GVAZLRB](?![\w]))?(?![\w.])",
}
# a date right after a birth marker (geb., Geburtsdatum, born, DOB, *): only the date is redacted (group v)
BUILTINS["BIRTHDATE"] = (r"(?i:\bgeb(?:oren|urtsdatum|\.)?|\bborn|\bDOB|\bdate of birth|\*)[:\s.]{0,3}(?:am\s|on\s)?"
                         r"(?P<v>" + BUILTINS["DATE"] + r")")

# Ligatures are expanded so "ﬁ" matches "fi"; whitespace kept so word gaps survive.
TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
INVISIBLE = dict.fromkeys(map(ord, "­​‌‍﻿"))


class RedactError(Exception):
    pass


# --------------------------------------------------------------------------- text model

class PageText:
    """Page text as one string with a bbox per character (unrotated page space)."""

    def __init__(self, page: pymupdf.Page):
        chars: list[str] = []
        boxes: list[tuple | None] = []
        lines: list[int] = []
        self.line_kind: list[str] = []
        d = page.get_text("rawdict", flags=TEXT_FLAGS)
        # Consecutive horizontal pieces on the same baseline with a small gap are one visual line (letter-spaced
        # text or one glyph per text operation would otherwise be a "line" per letter and never match a term).
        rows: list[list] = []  # [kind, [(c, bbox)], x1, baseline, height]
        for block in d["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                kind = line_kind(line.get("dir"))
                cs = [(ch["c"], tuple(ch["bbox"])) for span in line["spans"] for ch in span["chars"]
                      if ch["c"] not in "­​‌‍﻿"]
                if not cs:
                    continue
                x0, y0, x1, y1 = line["bbox"]
                h = max(y1 - y0, 1.0)
                prev = rows[-1] if rows else None
                if (kind == "h" and prev and prev[0] == "h" and abs(prev[3] - y1) < 0.35 * min(h, prev[4])
                        and -0.2 * h <= x0 - prev[2] < 1.5 * h):
                    if x0 - prev[2] > 0.25 * h:
                        prev[1].append((" ", None))
                    prev[1].extend(cs)
                    prev[2] = max(prev[2], x1)
                else:
                    rows.append([kind, cs, x1, y1, h])
        for line_no, (kind, cs, _, _, _) in enumerate(rows):
            self.line_kind.append(kind)
            for c, b in cs:
                chars.append(c)
                boxes.append(b)
                lines.append(line_no)
            chars.append("\n")
            boxes.append(None)
            lines.append(line_no)
        self.text = "".join(chars)
        self.boxes = boxes
        self.lines = lines

    def char_rects(self) -> list[pymupdf.Rect]:
        """Bounding boxes of all visible characters (to tell image text from text-layer text drawn over an image)."""
        return [pymupdf.Rect(b) for b, c in zip(self.boxes, self.text) if b is not None and not c.isspace()]

    def rects(self, start: int, end: int) -> list["Hit"]:
        """One Hit per text line covered by [start, end)."""
        by_line: dict[int, pymupdf.Rect] = {}
        for i in range(start, end):
            b = self.boxes[i]
            if b is None or self.text[i].isspace() and (i == start or i == end - 1):
                continue
            r = pymupdf.Rect(b)
            if r.is_empty:
                continue
            ln = self.lines[i]
            by_line[ln] = by_line[ln] | r if ln in by_line else r
        out = []
        for ln, r in by_line.items():
            kind = self.line_kind[ln]
            px, py = (PAD_Y, PAD_X) if kind == "v" else (PAD_X, PAD_Y)
            out.append(Hit(pymupdf.Rect(r.x0 - px, r.y0 - py, r.x1 + px, r.y1 + py), kind, pymupdf.Rect(r)))
        return out


class Hit:
    """rect: padded area (black box, image/line-art pass); kind: h|v|d line direction; core: matched glyph bboxes."""
    __slots__ = ("rect", "kind", "core")

    def __init__(self, rect, kind, core):
        self.rect, self.kind, self.core = rect, kind, core


# --------------------------------------------------------------------------- text inside images (OCR)

def area(r: pymupdf.Rect) -> float:
    return 0.0 if r.is_empty or r.is_infinite else r.width * r.height


def have_tesseract() -> bool:
    return shutil.which("tesseract") is not None


def image_regions(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Placed images worth OCRing (unrotated page space; caller sets rotation 0), full-page scans included."""
    pr = page.rect
    out = []
    try:
        infos = page.get_image_info()
    except Exception:
        return out
    for info in infos:
        r = pymupdf.Rect(info["bbox"]) & pr
        if r.is_empty or r.width < 20 or r.height < 8:
            continue
        if not any(abs(r.x0 - o.x0) < 0.5 and abs(r.y0 - o.y0) < 0.5 and abs(r.x1 - o.x1) < 0.5
                   and abs(r.y1 - o.y1) < 0.5 for o in out):
            out.append(r)
    return out


def looks_like_barcode(page: pymupdf.Page, r: pymupdf.Rect) -> bool:
    """Mostly pure black/white pixels with a balanced share of black: QR/Aztec/DataMatrix/1D barcode."""
    pix = page.get_pixmap(clip=r, dpi=150, colorspace=pymupdf.csGRAY)  # lower dpi blurs fine modules into grey
    data = pix.samples
    if len(data) < 400:
        return False
    dark = sum(1 for b in data if b < 80)
    light = sum(1 for b in data if b > 175)
    # text/graphics images stay far below 25% dark; real codes are ~35-55% dark with soft (JPEG) module edges
    return (dark + light) / len(data) > 0.8 and 0.25 < dark / len(data) < 0.75


def is_big(page: pymupdf.Page, r: pymupdf.Rect) -> bool:
    return r.width * r.height >= BIG_IMAGE * page.rect.width * page.rect.height


def osd_angle(png: str) -> int:
    """Tesseract orientation detection: degrees to rotate the image so that its text is upright (0/90/180/270)."""
    try:
        cp = subprocess.run(["tesseract", png, "-", "--psm", "0"], capture_output=True, text=True, timeout=OCR_TIMEOUT,
                            env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    except subprocess.TimeoutExpired:
        return 0
    m = re.search(r"Rotate: (\d+)", cp.stdout)
    c = re.search(r"Orientation confidence: ([\d.]+)", cp.stdout)
    return int(m.group(1)) if m and c and float(c.group(1)) >= 2.0 else 0


def ocr_dpi(page: pymupdf.Page, r: pymupdf.Rect) -> int:
    """Small images (fine print) and page images (scans) at 300 dpi; medium images (backgrounds, photos) at 200."""
    f = r.width * r.height / max(1.0, page.rect.width * page.rect.height)
    return 200 if 0.1 <= f < BIG_IMAGE else OCR_DPI


def _render(page: pymupdf.Page, r: pymupdf.Rect, angle: int, thr: int | None, png: str):
    z = ocr_dpi(page, r) / 72
    m = pymupdf.Matrix(z, z).prerotate(angle)
    pix = page.get_pixmap(matrix=m, clip=r, colorspace=pymupdf.csGRAY)
    if thr:
        lut = bytes(0 if i < thr else 255 for i in range(256))
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix.width, pix.height, pix.samples.translate(lut), 0)
    pix.save(png)
    return m, (pix.x, pix.y)


def ocr_jobs(page: pymupdf.Page, jobs: list[tuple]) -> dict:
    """jobs: [(key, rect, angle, threshold)] on one page -> see ocr_jobs_multi."""
    return ocr_jobs_multi([(key, page, r, a, t) for key, r, a, t in jobs])


def ocr_jobs_multi(jobs: list[tuple]) -> dict:
    """jobs: [(key, page, rect, angle, threshold)] (pages with rotation set to 0 by the caller)
    -> {key: [(word, Rect, line_key)]} in (unrotated) page space.
    Each region is rendered turned by `angle` (MuPDF, this thread only: it is not thread-safe); page-sized images get
    Tesseract's orientation detection first (scans lying sideways) and document layout (psm 3), smaller ones sparse
    text (psm 11). Tesseract runs in parallel."""
    if not jobs:
        return {}
    work = tempfile.mkdtemp(prefix="ocr-")
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}

    def tess(args):
        try:
            return subprocess.run(["tesseract", *args], capture_output=True, text=True, timeout=OCR_TIMEOUT,
                                  env=env).stdout
        except subprocess.TimeoutExpired:
            return ""

    try:
        rendered = {}
        for n, (key, page, r, angle, thr) in enumerate(jobs):
            png = os.path.join(work, f"j{n}.png")
            rendered[n] = (_render(page, r, angle, thr, png), png)
        big = [n for n, (_, page, r, _, _) in enumerate(jobs) if is_big(page, r)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            osd = dict(zip(big, ex.map(lambda n: tess([rendered[n][1], "-", "--psm", "0"]), big)))
            for n, out in osd.items():
                m_ = re.search(r"Rotate: (\d+)", out)
                c_ = re.search(r"Orientation confidence: ([\d.]+)", out)
                extra = int(m_.group(1)) if m_ and c_ and float(c_.group(1)) >= 2.0 else 0
                if extra:
                    key, page, r, angle, thr = jobs[n]
                    rendered[n] = (_render(page, r, (angle + extra) % 360, thr, rendered[n][1]), rendered[n][1])
            outs = list(ex.map(lambda n: tess([rendered[n][1], "-", "-l", OCR_LANGS, "--psm",
                                               "3" if n in osd else "11", "tsv"]), range(len(jobs))))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result: dict = {}
    for n, out in enumerate(outs):
        key, page, r, angle, thr = jobs[n]
        (m, (ox, oy)), _ = rendered[n]
        inv = ~m
        words = result.setdefault(key, [])
        for ln in out.splitlines()[1:]:
            f = ln.split("\t")
            if len(f) < 12 or f[0] != "5" or not f[11].strip():
                continue
            try:
                if float(f[10]) < OCR_MIN_CONF:
                    continue
            except ValueError:
                continue
            x, y, w, h = map(int, f[6:10])
            rect = (pymupdf.Rect(ox + x, oy + y, ox + x + w, oy + y + h) * inv) & r  # back to page space
            if not rect.is_empty:
                words.append((f[11].strip(), rect, (n, f[2], f[3], f[4])))
    return result


def ocr_region(page: pymupdf.Page, r: pymupdf.Rect, work: str = "", idx=0, thr: int | None = None,
               angle: int = 0) -> list[tuple]:
    return ocr_jobs(page, [(0, r, angle, thr)]).get(0, [])


class ImageText:
    """OCR text of the images on a page as one string with an (estimated) box per character, like PageText.
    Caller sets rotation 0. regions: image rects; barcodes: the subset that looks like a barcode/QR code."""

    def __init__(self, page: pymupdf.Page, regions: list[pymupdf.Rect] | None = None, detect_barcodes: bool = True,
                 rot: int = 0, prefetched: dict | None = None, barcodes: list | None = None):
        self.regions = image_regions(page) if regions is None else regions
        if barcodes is not None:
            self.barcodes = barcodes
        else:
            self.barcodes = [r for r in self.regions if looks_like_barcode(page, r)] if detect_barcodes else []
        chars, boxes, lines = [], [], []
        self.line_kind: list[str] = []
        self.region_lines: list[tuple[int, str]] = []  # (region index, line text) for extract
        self.region_words: dict[int, int] = {}
        self.line_region: list[int] = []  # line number -> region index
        todo = [(k, r) for k, r in enumerate(self.regions) if r not in self.barcodes] if have_tesseract() else []
        todo = todo[:OCR_MAX_IMAGES]
        # rotated page: read every image as stored AND as displayed (scans are stored lying and shown upright via
        # /Rotate; landscape content is drawn upright and shown turned)
        if prefetched is None:
            prefetched = ocr_jobs(page, image_jobs(page, todo, rot))
        results = [(k, prefetched.get(k, [])) for k, _ in todo]
        line_no = 0
        for k, words in results:
            self.region_words[k] = len(words)
            grouped: dict = {}
            for w, rect, key in words:
                grouped.setdefault(key, []).append((w, rect))
            for key in grouped:
                ws = grouped[key]  # tesseract reading order
                self.line_kind.append("h")
                self.line_region.append(k)
                self.region_lines.append((k, " ".join(w for w, _ in ws)))
                for j, (w, rect) in enumerate(ws):
                    cw = rect.width / max(1, len(w))
                    for n, c in enumerate(w):
                        chars.append(c)
                        boxes.append((rect.x0 + n * cw, rect.y0, rect.x0 + (n + 1) * cw, rect.y1))
                        lines.append(line_no)
                    if j < len(ws) - 1:
                        chars.append(" ")
                        boxes.append(None)
                        lines.append(line_no)
                chars.append("\n")
                boxes.append(None)
                lines.append(line_no)
                line_no += 1
        self.ocr_ran = bool(todo)
        self.text = "".join(chars)
        self.boxes = boxes
        self.lines = lines

    rects = PageText.rects


def image_jobs(page: pymupdf.Page, todo: list[tuple], rot: int, key=lambda k: k) -> list[tuple]:
    """OCR jobs for ImageText. Rotated page: every image as stored AND as displayed (scans are stored lying and shown
    upright via /Rotate; landscape content is drawn upright and shown turned); page images use OSD instead."""
    angles = [0] + ([rot] if rot else [])
    return [(key(k), r, a, None) for k, r in todo for a in (angles if not is_big(page, r) else [rot])]


def image_texts(doc, regions_by_page: dict | None = None, detect_barcodes: bool = True, pages=None) -> dict:
    """ImageText for many pages with ONE parallel OCR batch (keeps all cores busy). regions_by_page: {page_index:
    [Rect]} (default: every page, its images). Pages are temporarily set to rotation 0."""
    idx = list(regions_by_page) if regions_by_page is not None else list(pages if pages is not None
                                                                          else range(doc.page_count))
    rots = {}
    plans = {}
    jobs = []
    try:
        for i in idx:
            page = doc[i]
            rots[i] = page.rotation
            if rots[i]:
                page.set_rotation(0)
            regions = image_regions(page) if regions_by_page is None else regions_by_page[i]
            barcodes = [r for r in regions if looks_like_barcode(page, r)] if detect_barcodes else []
            todo = [(k, r) for k, r in enumerate(regions) if r not in barcodes][:OCR_MAX_IMAGES] if have_tesseract() else []
            plans[i] = (regions, barcodes)
            jobs += [(key, page, r, a, t) for key, r, a, t in image_jobs(page, todo, rots[i], key=lambda k, i=i: (i, k))]
        found = ocr_jobs_multi(jobs)
        out = {}
        for i in idx:
            regions, barcodes = plans[i]
            pref = {k: w for (pi, k), w in found.items() if pi == i}
            out[i] = ImageText(doc[i], regions=regions, rot=rots[i], prefetched=pref, barcodes=barcodes)
        return out
    finally:
        for i, r in rots.items():
            if r:
                doc[i].set_rotation(r)


def over_text(r: pymupdf.Rect, text_rects: list) -> bool:
    """OCR box lies over text-layer characters (the render of an image region includes text drawn on top)."""
    a = area(r)
    return a > 0 and sum(area(r & t) for t in text_rects if r.intersects(t)) > 0.4 * a


def data_bearing_regions(it: "ImageText", page_text: str, text_rects: list | None = None) -> list[pymupdf.Rect]:
    """Images that repeat the document's data: >= 3 distinct words (>= 4 letters) or numbers (>= 6 digits) of the
    page's text layer appear in their OCR text (ticket/ID security backgrounds print name, route, order number ...
    as a faint pattern that OCR reads only partly)."""
    page_tokens = set(re.findall(r"[^\W\d_]{4,}|\d{6,}", page_text.casefold()))
    out = []
    by_region: dict[int, str] = defaultdict(str)
    word, wbox, wline = "", None, -1
    for c, b, ln in list(zip(it.text, it.boxes, it.lines)) + [(" ", None, -1)]:
        if b is not None and not c.isspace():
            word += c
            wbox = pymupdf.Rect(b) if wbox is None else wbox | pymupdf.Rect(b)
            wline = ln
            continue
        if word and not over_text(wbox, text_rects or []):
            by_region[it.line_region[wline]] += " " + word
        word, wbox = "", None
    for k, txt in by_region.items():
        shared = set(re.findall(r"[^\W\d_]{4,}|\d{6,}", txt.casefold())) & page_tokens
        if len(shared) >= 3:
            out.append(it.regions[k])
    return out


def compact(s: str) -> str:
    """casefold, letters and digits only (OCR spacing/punctuation differences do not matter)."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s).casefold())


def scan_targets(terms, values, pieces: bool = True) -> list[str]:
    """What must not be readable anywhere in an image after redaction, in compact() form: every term, every word
    (>= 4 letters) of a multi-word term, every matched label/regex/builtin value and, for long numbers, any 5-digit
    piece of them (OCR often reads only part of a faint or rotated number)."""
    out = set()
    for t in terms or []:
        out.add(compact(t))
        if pieces:
            out.update(compact(w) for w in t.split() if len(compact(w)) >= 4)
    for v in values or []:
        c = compact(v)
        out.add(c)
        if pieces and c.isdigit() and len(c) >= 10:
            out.update(c[i:i + 5] for i in range(len(c) - 4))
    return sorted(x for x in out if len(x) >= 3)


def scan_regions(page: pymupdf.Page, regions: list[pymupdf.Rect], targets: list[str], rot: int = 0) -> list[bool]:
    return scan_regions_multi([(page, r, rot) for r in regions], targets)


def scan_regions_multi(items: list[tuple], targets: list[str]) -> list[bool]:
    """items: [(page, rect, rot)] (pages at rotation 0) -> does any target (compact form) appear in the region?
    Every region is OCRed in SCAN_VARIANTS (rotated +-10 deg, contrast threshold for light grey, mirrored), as stored
    and as displayed, until a variant reveals a target. All regions of all pages go into one batch per variant
    (rendering in this thread - MuPDF is not thread-safe -, tesseract in parallel)."""
    hits = [False] * len(items)
    work = tempfile.mkdtemp(prefix="scan-")

    def run_one(job):
        k, png = job
        try:
            out = subprocess.run(["tesseract", png, "-", "-l", OCR_LANGS, "--psm", "11"], capture_output=True,
                                 text=True, timeout=OCR_TIMEOUT, env={**os.environ, "OMP_THREAD_LIMIT": "1"}).stdout
        except subprocess.TimeoutExpired:
            out = ""
        return k, compact(out)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            for n, (ang, thr, mirror) in enumerate(SCAN_VARIANTS):
                jobs = []
                for k, (page, r, rot) in enumerate(items):
                    if hits[k]:
                        continue
                    for base in ([0, rot] if rot else [0]):  # as stored and as displayed
                        z = ocr_dpi(page, r) / 72
                        m = pymupdf.Matrix(z, z).prerotate(base + ang)
                        if mirror:
                            m = m * pymupdf.Matrix(-1, 0, 0, 1, 0, 0)
                        pix = page.get_pixmap(matrix=m, clip=r, colorspace=pymupdf.csGRAY)
                        if thr:
                            lut = bytes(0 if i < thr else 255 for i in range(256))
                            pix = pymupdf.Pixmap(pymupdf.csGRAY, pix.width, pix.height, pix.samples.translate(lut), 0)
                        png = os.path.join(work, f"r{k}v{n}b{base}.png")
                        pix.save(png)
                        jobs.append((k, png))
                if not jobs:
                    break
                for k, text in ex.map(run_one, jobs):
                    if any(x in text for x in targets):
                        hits[k] = True
                for _, png in jobs:
                    os.remove(png)
        return hits
    finally:
        shutil.rmtree(work, ignore_errors=True)


def is_blank(page: pymupdf.Page, r: pymupdf.Rect) -> bool:
    """Region is (almost) one flat colour after redaction - nothing left to read."""
    data = page.get_pixmap(clip=r, dpi=50, colorspace=pymupdf.csGRAY).samples
    return not data or max(data) - min(data) < 25


def value_regex(v: str) -> re.Pattern:
    """A matched label/regex/builtin value as a literal, tolerant to spacing (OCR splits long numbers). A value that
    is a name (2-3 plain words) is searched like a --terms name (also inverted: "Musterfrau, Erika")."""
    if re.fullmatch(r"[^\W\d_]+(?:[-'][^\W\d_]+)*(?: [^\W\d_]+(?:[-'][^\W\d_]+)*){1,2}", v.strip()):
        return term_regex(v.strip())
    chars = [re.escape(c) for c in re.sub(r"\s+", "", v)]
    return re.compile(r"(?<![\w])" + r"\s*".join(chars) + r"(?![\w])", re.IGNORECASE)


def text_layer_values(doc, matchers) -> list[tuple[str, str]]:
    """(label, value) for every --label/--regex/--builtin match in the text layer of the whole document."""
    vals: dict[str, str] = {}
    pattern_matchers = [(lb, rx) for lb, rx in matchers if lb.startswith(("label:", "regex:", "builtin:"))]
    if not pattern_matchers:
        return []
    for page in doc:
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        text = PageText(page).text
        if rot:
            page.set_rotation(rot)
        for lb, rx in pattern_matchers:
            for m in rx.finditer(text):
                v = text[slice(*match_span(m))].strip()
                if len(re.sub(r"\s+", "", v)) >= 4:
                    vals.setdefault(re.sub(r"\s+", " ", v), lb)
    return [(lb, v) for v, lb in vals.items()]


def line_kind(d) -> str:
    """h = horizontal, v = vertical (either way), d = diagonal/unknown."""
    if not d:
        return "d"
    c, s_ = abs(d[0]), abs(d[1])
    if s_ < 1e-3:
        return "h"
    if c < 1e-3:
        return "v"
    return "d"


def char_centres(page: pymupdf.Page):
    """(char, x, y) for every non-space char, unrotated page space (caller sets rotation 0)."""
    d = page.get_text("rawdict", flags=TEXT_FLAGS)
    for block in d["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                for ch in span["chars"]:
                    if ch["c"].isspace() or ch["c"] in "­​‌‍﻿":
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    yield ch["c"], (x0 + x1) / 2, (y0 + y1) / 2


def term_regex(term: str) -> re.Pattern:
    """Case-insensitive, word-boundary, any run of whitespace between words; single spaces between the letters of
    a word are tolerated (letter-spaced text: "M ü l l e r"). A name of 2-3 plain words also matches inverted
    ("Erika Musterfrau" -> "Musterfrau, Erika", as in medical records and forms)."""
    words = term.translate(INVISIBLE).split()
    if not words:
        raise RedactError(f"empty term {term!r}")

    def seq(ws, first_sep=r"\s+"):
        parts = ["[ ]?".join(re.escape(c) for c in w) for w in ws]
        return parts[0] + (first_sep + r"\s+".join(parts[1:]) if len(parts) > 1 else "")

    body = seq(words)
    if 2 <= len(words) <= 3 and all(re.fullmatch(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", w) for w in words):
        body = f"(?:{body}|{seq([words[-1]] + words[:-1], r',?\s+')})"
    return re.compile(r"(?<![\w])" + body + r"(?![\w])", re.IGNORECASE)


def split_builtins(builtins) -> list[str]:
    """Accepts "EMAIL", "email", "EMAIL,IBAN"."""
    return [x.strip().upper() for b in builtins or [] for x in b.split(",") if x.strip()]


def label_regex(label: str) -> re.Pattern:
    """The value after a label on the same line (group v), e.g. "Auftragsnummer: 318204657731" -> the number,
    "Patient: Erika Musterfrau, geb. ..." -> "Erika Musterfrau" (up to 4 capitalised words), "IBAN: DE89 3704 ..." ->
    the code with its digit groups. A following "Other label:" is never taken."""
    words = label.translate(INVISIBLE).split()
    if not words:
        raise RedactError(f"empty label {label!r}")
    body = r"\s+".join(re.escape(w) for w in words)
    up = "A-ZÄÖÜÀ-ÖØ-Þ"
    name_word = rf"(?-i:[{up}][^\W\d_]+(?:[-'][{up}]?[^\W\d_]+)*)"
    not_label = r"(?![^\s]*:)"
    end = r"(?![\w\-/.])"
    name = rf"{name_word}{not_label}(?:[ ]+(?:(?-i:von|van|de|zu|der|den)|{name_word}){not_label}){{0,2}}{end}"
    # an address line: street (+ more words) + house number [, postcode city]
    street = (rf"{name_word}(?:[ ]+{name_word}){{0,2}}[ ]+\d{{1,4}}(?-i:[a-z])?(?:-\d{{1,4}})?"
              rf"(?:,?[ ]+\d{{4,5}}[ ]+{name_word}(?:[ ]+{name_word})?)?{end}")
    code = rf"[^\W_][^\s]*?(?:[ ](?-i:[0-9A-Z]{{1,6}}){not_label}(?=[\s,;]|$))*(?=[\s,;]|$)"
    titles = r"(?:(?:Dr|Prof|med|dent|Dipl|Ing|PD|Herr|Herrn|Frau|Mr|Mrs|Ms)\.?(?:-\w+\.?)?[ \t]+)*"
    return re.compile(r"(?<![\w])" + body + r"\w*[ \t]*[.:#]*[ \t]*(?:(?:Nr|No)\b[ \t]*[.:#]*[ \t]*)?"
                      + titles + rf"(?P<v>{street}|{name}|{code})", re.IGNORECASE)


def match_span(m: re.Match) -> tuple[int, int]:
    """Span to redact: group v (label value) when the pattern has one, else the whole match."""
    return m.span("v") if "v" in m.re.groupindex else m.span()


def build_matchers(terms, regexes, builtins, labels=None) -> list[tuple[str, re.Pattern]]:
    out = []
    for t in terms or []:
        out.append((t, term_regex(t)))
    for lb in labels or []:
        out.append((f"label:{lb}", label_regex(lb)))
    for r in regexes or []:
        try:
            out.append((f"regex:{r}", re.compile(r)))
        except re.error as e:
            raise RedactError(f"invalid regex {r!r}: {e}")
    for b in split_builtins(builtins):
        key = b
        if key not in BUILTINS:
            raise RedactError(f"unknown builtin {b!r}; available: {', '.join(BUILTINS)}")
        out.append((f"builtin:{key}", re.compile(BUILTINS[key])))
    if not out:
        raise RedactError("nothing to redact: give --terms, --label, --regex or --builtin")
    return out


def union_area(rects: list[pymupdf.Rect]) -> float:
    rects = [r for r in rects if not r.is_empty]
    if not rects:
        return 0.0
    if len(rects) > 200:
        return sum(r.width * r.height for r in rects)
    xs = sorted({v for r in rects for v in (r.x0, r.x1)})
    area = 0.0
    for xa, xb in zip(xs, xs[1:]):
        spans = sorted((r.y0, r.y1) for r in rects if r.x0 <= xa and r.x1 >= xb)
        cov, cur = 0.0, None
        for y0, y1 in spans:
            if cur is None or y0 > cur[1]:
                if cur:
                    cov += cur[1] - cur[0]
                cur = [y0, y1]
            else:
                cur[1] = max(cur[1], y1)
        if cur:
            cov += cur[1] - cur[0]
        area += cov * (xb - xa)
    return area


def image_coverage(page: pymupdf.Page) -> float:
    pr = page.rect
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    boxes = [pymupdf.Rect(i["bbox"]) & pr for i in infos if i.get("bbox")]
    total = pr.width * pr.height
    return min(1.0, union_area(boxes) / total) if total else 0.0


def page_flags(page: pymupdf.Page, text: str) -> dict:
    n = len(text.strip())
    images = page.get_images(full=False)
    no_text = n == 0
    cov = image_coverage(page) if images else 0.0
    # Image-only page, or a page dominated by images with almost no text: likely a scan.
    needs_ocr = bool(images) and n < 20
    # Images cover half the page or more: text INSIDE those images is invisible to this tool.
    image_heavy = not needs_ocr and cov >= IMAGE_HEAVY
    return {"chars": n, "images": len(images), "image_cover": round(cov, 2), "no_text": no_text,
            "needs_ocr": needs_ocr, "image_heavy": image_heavy}


def open_pdf(path: str) -> pymupdf.Document:
    if not os.path.isfile(path):
        raise RedactError(f"file not found: {path}")
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise RedactError(f"cannot open {path}: {e}")
    if not doc.is_pdf:
        raise RedactError(f"{path} is not a PDF")
    if doc.needs_pass:
        raise RedactError(f"{path} is password protected")
    return doc


# --------------------------------------------------------------------------- extract

def parse_pages(spec: str | None, n: int) -> set[int]:
    if not spec:
        return set(range(1, n + 1))
    out = set()
    for part in spec.split(","):
        a_, _, b_ = part.strip().partition("-")
        try:
            lo = int(a_)
            hi = int(b_) if b_ else lo
        except ValueError:
            raise RedactError(f"invalid --pages {spec!r} (use e.g. 3 or 2-5)")
        out.update(range(max(1, lo), min(n, hi) + 1))
    return out


def useful_ocr_line(ln: str, known_words: set) -> bool:
    """Image text worth showing: mostly letters (not security-pattern noise) and at least one word that is not
    already in the text layer (names that also stand in the text layer are found in images by apply anyway)."""
    compact = ln.replace(" ", "")
    letters = sum(c.isalpha() for c in compact)
    if letters < 4 or letters / max(1, len(compact)) < 0.6:
        return False
    words = re.findall(r"[^\W\d_]{3,}", ln.lower())
    return any(w not in known_words for w in words)


def cmd_extract(a) -> int:
    doc = open_pdf(a.file)
    wanted = parse_pages(a.pages, doc.page_count)
    pages = []
    its = image_texts(doc, pages=[i for i in range(doc.page_count) if i + 1 in wanted])
    for i, page in enumerate(doc):
        if i + 1 not in wanted:
            continue
        text = page.get_text("text", flags=TEXT_FLAGS).translate(INVISIBLE)
        text = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        it = its[i]
        img_lines = [ln for _, ln in it.region_lines]
        barcodes = [[round(v) for v in r * page.rotation_matrix] for r in it.barcodes]
        pages.append({"page": i + 1, **page_flags(page, text), "text": text, "image_text": "\n".join(img_lines),
                      "images_ocr": it.ocr_ran, "barcodes": barcodes})
    ocr_pages = [p["page"] for p in pages if p["needs_ocr"] or p["no_text"]]
    heavy = [p["page"] for p in pages if p["image_heavy"]]
    if a.json:
        print(json.dumps({"file": a.file, "pages": len(pages), "needs_ocr_pages": ocr_pages,
                          "image_heavy_pages": heavy, "full_app": (FULL_APP or None) if ocr_pages or heavy else None,
                          "page_texts": pages}, ensure_ascii=False))
        return 0
    print(f"# {a.file}: {doc.page_count} page(s)" + (f", showing {a.pages}" if a.pages else ""))
    if not a.full:
        print("(Compact view: lines repeated from earlier pages and image text already in the text layer are shown "
              "once. apply/verify always process ALL pages and every occurrence, also inside images.)")
    known_words = set(re.findall(r"[^\W\d_]{3,}", " ".join(p["text"] for p in pages).lower()))
    seen_lines: set = set()
    used = 0
    for n_, p in enumerate(pages):
        if not a.full and used > EXTRACT_MAX_CHARS:
            rest = [q["page"] for q in pages[n_:]]
            rng = f"{rest[0]}-{rest[-1]}" if len(rest) > 1 else str(rest[0])
            print(f"\n[OUTPUT LIMIT: page(s) {rng} not shown. Run `redact extract \"{a.file}\" --pages {rng}` to read "
                  f"them before deciding the targets.]")
            break
        if not a.full:
            body, dup = [], 0
            for ln in p["text"].splitlines():
                key = ln.strip().lower()
                if key and key in seen_lines:
                    dup += 1
                    continue
                seen_lines.add(key)
                body.append(ln)
            if dup:
                body.append(f"({dup} line(s) repeated from earlier pages omitted)")
            imgs, noise = [], 0
            for ln in p["image_text"].splitlines():
                key = "img:" + ln.strip().lower()
                if key in seen_lines or not useful_ocr_line(ln, known_words):
                    noise += 1
                    continue
                seen_lines.add(key)
                imgs.append(ln)
            p = {**p, "text": "\n".join(body).strip(), "image_text": "\n".join(imgs)}
            if noise and not imgs:
                p["image_text"] = ""
        used += len(p["text"]) + len(p["image_text"])
        flag = ""
        if p["needs_ocr"]:
            flag = "  [SCANNED/IMAGE PAGE - no text layer; the text below comes from OCR and may contain misreads]"
        elif p["image_heavy"]:
            flag = f"  [MOSTLY IMAGES ({int(p['image_cover'] * 100)}% of the page) - their text comes from OCR]"
        elif p["no_text"]:
            flag = "  [no text]"
        print(f"\n=== page {p['page']} ({p['chars']} chars){flag} ===")
        if p["text"]:
            print(p["text"])
        if p["image_text"]:
            print(f"\n--- page {p['page']}: text INSIDE images (OCR; may contain misreads; redact handles it) ---")
            print(p["image_text"])
        for b in p["barcodes"]:
            print(f"[barcode/QR code at {b}: kept unless the user explicitly asks to black out barcodes (--barcodes)]")
    if ocr_pages or heavy:
        print(f"\nNOTE: page(s) {', '.join(map(str, sorted(set(ocr_pages + heavy))))} are read by OCR (scan or "
              f"mostly images). OCR can misread unusual fonts or handwriting: tell the user to look over those pages."
              + (f" Full app for difficult scans: {FULL_APP}" if FULL_APP else ""))
    return 0


# --------------------------------------------------------------------------- apply

def run(cmd: list[str], timeout=GS_TIMEOUT) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r


def run_gs(args: list[str], expected: str | None):
    r = run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", *args])
    if r.returncode != 0 or (expected and (not os.path.isfile(expected) or os.path.getsize(expected) == 0)):
        raise RedactError(f"Ghostscript failed (exit {r.returncode}): {(r.stderr + r.stdout)[-500:]}")


def text_band(r: pymupdf.Rect, kind: str = "h") -> pymupdf.Rect:
    """Text-removal area: inset only perpendicular to the line direction (keeps neighbouring lines intact).
    Horizontal line -> trim height; vertical line -> trim width; diagonal/unknown -> full rect."""
    if kind == "h":
        inset = r.height * TEXT_BAND_INSET
        return pymupdf.Rect(r.x0, r.y0 + inset, r.x1, r.y1 - inset)
    if kind == "v":
        inset = r.width * TEXT_BAND_INSET
        return pymupdf.Rect(r.x0 + inset, r.y0, r.x1 - inset, r.y1)
    return pymupdf.Rect(r)


TITLES = {"dr", "prof", "herr", "herrn", "frau", "mr", "mrs", "ms", "miss", "sir", "dipl", "ing", "von", "van",
          "der", "den", "de", "la", "le", "und", "and", "the", "gmbh", "ag", "kg", "inc", "ltd"}


def name_parts(term: str) -> list[str]:
    """Words of a multi-word term that could stand alone (e.g. a surname)."""
    words = [w.strip(".,;:()") for w in term.split()]
    if len(words) < 2:
        return []
    return [w for w in words if len(w) >= 3 and w[:1].isupper() and w.isalpha() and w.lower() not in TITLES]


TITLE_BEFORE = re.compile(r"(?:\b(?:Herrn?|Frau|Dr|Prof|Mr|Mrs|Ms|Miss|Mme|Mlle)\.?\s+)+$", re.IGNORECASE)


UP = "A-ZÄÖÜÀ-ÖØ-Þ"
STREET_LINE = re.compile(rf"^(?:[{UP}][^\s\d,]*\.?[ \-]+){{0,3}}[{UP}][^\s\d,]*\.?[ ]+\d{{1,4}}(?:[ ]?[a-zA-Z])?(?:[-/]\d{{1,4}})?"
                         rf"(?:,?[ ]+(?:[A-Z]{{1,2}}-)?\d{{4,5}}[ ]+[{UP}][^\d]*)?$"
                         rf"|^\d{{1,5}}[ ]+[{UP}][\w.\- ]+$|^(?i:postfach|p\.?o\.? box)[ ]+\d+$")
CITY_LINE = re.compile(rf"^(?:[A-Z]{{1,2}}-)?\d{{4,5}}[ ]+[{UP}][^\d]*$|^[{UP}][\w .\-]+,[ ]+[A-Z]{{2}}[ ]+\d{{5}}$")


ADDRESS_LABEL = re.compile(r"^(?i:anschrift|adresse|wohnanschrift|wohnort|postanschrift|address|home address)"
                           r"\s*[:.]?\s*(?=\S)")
DATA_LINE = re.compile(r"^[^\W\d][^:]{0,30}:\s*\S")  # "Versichertennummer: ..." - a data block keeps going


def address_spans(text: str, end: int) -> list[tuple[int, int]]:
    """--address: the postal address of the person whose name ends at `end`: the lines right below it that are a
    street + number, postcode + city or P.O. box, or a labelled address field ("Anschrift: ...") within the next
    4 lines of a data block ("Label: value" lines are skipped). -> character spans."""
    spans = []
    i = text.find("\n", end)
    for _ in range(4):
        if i < 0:
            break
        j = text.find("\n", i + 1)
        j = len(text) if j < 0 else j
        raw = text[i + 1:j]
        line = raw.strip()
        lab = ADDRESS_LABEL.match(line)
        if lab:
            a_ = i + 1 + raw.index(line) + lab.end()
            spans.append((a_, i + 1 + raw.index(line) + len(line)))
        elif line and (STREET_LINE.match(line) or CITY_LINE.match(line)):
            a_ = i + 1 + raw.index(line)
            spans.append((a_, a_ + len(line)))
        elif not (line and not spans and DATA_LINE.match(line)):
            break
        i = j if j < len(text) else -1
    return spans


def find_hits(doc, matchers, hints=None, auto=None, barcodes=False, images=None, address=False, found_text=None):
    """-> {page_index: [Hit]}, Counter[(label, page)], [ocr pages], [image-heavy pages]
    hints (list) receives parts of multi-word terms that still occur outside any match.
    auto (list, optional): parts preceded by a title (Herr/Frau/Dr./Mr. ...) are redacted too and listed here.
    Text inside images is OCRed and matched too; barcodes=True blacks out every barcode/QR-like image.
    images (dict, optional) receives {page_index: {"regions": [Rect], "barcodes": [Rect], "ocr": bool, "hits": n}}."""
    rects: dict[int, list] = defaultdict(list)
    counts: Counter = Counter()
    ocr_pages, heavy_pages = [], []
    leftovers: Counter = Counter()
    contexts: dict = {}
    # values found by --label/--regex/--builtin are searched literally everywhere else too (an order number that is
    # printed again without its label, or inside an image)
    values = [(lb, value_regex(v)) for lb, v in text_layer_values(doc, matchers)]
    its = image_texts(doc)  # all pages in one OCR batch
    for i, page in enumerate(doc):
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        pt = PageText(page)
        it = its[i]
        if rot:
            page.set_rotation(rot)
        fl = page_flags(page, pt.text)
        if fl["needs_ocr"]:
            ocr_pages.append(i + 1)
        if fl["image_heavy"]:
            heavy_pages.append(i + 1)
        covered = bytearray(len(pt.text))
        for label, rx in matchers:
            for m in rx.finditer(pt.text):
                a_, b_ = match_span(m)
                if b_ <= a_:
                    continue
                rs = pt.rects(a_, b_)
                if rs:
                    rects[i].extend(rs)
                    counts[(label, i + 1)] += 1
                    covered[a_:b_] = b"\1" * (b_ - a_)
        if address:
            name_ends = [m.end() for lb, rx in matchers if not lb.startswith(("label:", "regex:", "builtin:"))
                         for m in rx.finditer(pt.text)]
            for e in name_ends:
                for a_, b_ in address_spans(pt.text, e):
                    if all(covered[a_:b_]):
                        continue
                    rs = pt.rects(a_, b_)
                    if rs:
                        rects[i].extend(rs)
                        counts[("address", i + 1)] += 1
                        covered[a_:b_] = b"\1" * (b_ - a_)
                        if found_text is not None:
                            found_text.append(pt.text[a_:b_])
        for label, rx in values:
            for m in rx.finditer(pt.text):
                if m.end() <= m.start() or any(covered[m.start():m.end()]):
                    continue
                rs = pt.rects(m.start(), m.end())
                if rs:
                    rects[i].extend(rs)
                    counts[(label, i + 1)] += 1
                    covered[m.start():m.end()] = b"\1" * (m.end() - m.start())
        text_cores = [h.core for h in rects[i]]
        text_rects = pt.char_rects()
        img_hits = 0
        ocr_values = []
        hit_regions: set = set()
        it_covered = bytearray(len(it.text))
        for label, rx in matchers + values:
            for m in rx.finditer(it.text):
                a_, b_ = match_span(m)
                if b_ <= a_:
                    continue
                it_covered[a_:b_] = b"\1" * (b_ - a_)
                hr = [h for h in it.rects(a_, b_) if not over_text(h.core, text_rects)]
                if hr:
                    hit_regions.add(it.line_region[it.lines[a_]])
                if label.startswith(("label:", "regex:", "builtin:")):
                    ocr_values.append(it.text[a_:b_])
                for h in it.rects(a_, b_):
                    # same spot already found in the text layer (text drawn over an image): not a second hit
                    if any(area(h.core & c) > 0.5 * area(h.core) for c in text_cores):
                        continue
                    rects[i].append(h)
                    counts[(label, i + 1)] += 1
                    img_hits += 1
        # same title rule as for the text layer: "Herr Dr. Müller" after "Thomas Müller" is the same person
        if auto is not None:
            for label, _ in matchers:
                for part in name_parts(label):
                    part_rx = re.compile(r"(?<![\w.@/\-])" + re.escape(part) + r"(?![\w@/\-]|\.\w)", re.IGNORECASE)
                    for m in part_rx.finditer(it.text):
                        if any(it_covered[m.start():m.end()]):
                            continue
                        if TITLE_BEFORE.search(it.text[max(0, m.start() - 40):m.start()]):
                            it_covered[m.start():m.end()] = b"\1" * (m.end() - m.start())
                            hs = [h for h in it.rects(m.start(), m.end())
                                  if not any(area(h.core & c) > 0.5 * area(h.core) for c in text_cores)]
                            if hs:
                                rects[i].extend(hs)
                                counts[(label, i + 1)] += 1
                                img_hits += 1
                                auto.append({"word": part, "from_term": label, "page": i + 1})
        if address:
            for lb, rx in matchers:
                if lb.startswith(("label:", "regex:", "builtin:")):
                    continue
                for m in rx.finditer(it.text):
                    for a_, b_ in address_spans(it.text, m.end()):
                        if all(it_covered[a_:b_]):
                            continue
                        hs = [h for h in it.rects(a_, b_)
                              if not any(area(h.core & c) > 0.5 * area(h.core) for c in text_cores)]
                        if hs:
                            rects[i].extend(hs)
                            counts[("address", i + 1)] += 1
                            img_hits += 1
                            hit_regions.update(it.line_region[it.lines[a_]] for _ in [0])
                            it_covered[a_:b_] = b"\1" * (b_ - a_)
                            if found_text is not None:
                                found_text.append(it.text[a_:b_])
        if barcodes:
            for r in it.barcodes:
                rects[i].append(Hit(pymupdf.Rect(r), "h", pymupdf.Rect(r)))
                counts[("barcodes", i + 1)] += 1
        if images is not None:
            images[i] = {"regions": it.regions, "barcodes": it.barcodes, "ocr": it.ocr_ran, "hits": img_hits,
                         "text_regions": [r for k, r in enumerate(it.regions) if it.region_words.get(k)],
                         "values": ocr_values,
                         "hit_regions": [it.regions[k] for k in sorted(hit_regions)],
                         "data_regions": data_bearing_regions(it, pt.text, text_rects),
                         "page_has_hits": bool(rects[i]) or img_hits > 0}
        for label, _ in matchers:
            for part in name_parts(label):
                # standalone word only: not part of an e-mail address, URL, file name or hyphenated name
                part_rx = re.compile(r"(?<![\w.@/\-])" + re.escape(part) + r"(?![\w@/\-]|\.\w)", re.IGNORECASE)
                for m in part_rx.finditer(pt.text):
                    if any(covered[m.start():m.end()]):
                        continue
                    if auto is not None and TITLE_BEFORE.search(pt.text[max(0, m.start() - 40):m.start()]):
                        # "Herr Dr. Müller" after redacting "Thomas Müller": same person, redact too.
                        rs = pt.rects(m.start(), m.end())
                        rects[i].extend(rs)
                        counts[(label, i + 1)] += 1
                        covered[m.start():m.end()] = b"\1" * (m.end() - m.start())
                        auto.append({"word": part, "from_term": label, "page": i + 1})
                        continue
                    if True:
                        key = (part, label, i + 1)
                        leftovers[key] += 1
                        ctx = pt.text[max(0, m.start() - 25):m.end() + 25].replace("\n", " ").strip()
                        contexts.setdefault(key, []).append(ctx)
    if hints is not None:
        for (part, label, page), n in sorted(leftovers.items(), key=lambda kv: kv[0][2]):
            hints.append({"word": part, "from_term": label, "page": page, "count": n,
                          "contexts": contexts[(part, label, page)][:3]})
    return rects, counts, ocr_pages, heavy_pages


def matched_values(path: str, regexes, builtins, labels=None) -> list[str]:
    """Text matched by --regex/--builtin/--label in the ORIGINAL file (kept in memory only; used by verify)."""
    ms = [label_regex(lb) for lb in labels or []]
    for r in regexes or []:
        ms.append(re.compile(r))
    for b in split_builtins(builtins):
        ms.append(re.compile(BUILTINS[b]))
    if not ms:
        return []
    doc = open_pdf(path)
    vals = set()
    for page in doc:
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        text = PageText(page).text
        for rx in ms:
            for m in rx.finditer(text):
                v = normalize(text[slice(*match_span(m))])
                digits_only = re.fullmatch(r"[\d\s.,'’/\-]+", v) is not None
                if len(v) >= (6 if digits_only else 4):
                    vals.add(v)
    doc.close()
    return sorted(vals)


def sanitize(doc: pymupdf.Document, keep_optional_content=False):
    doc.scrub(attached_files=True, clean_pages=True, embedded_files=True, hidden_text=True, javascript=True,
              metadata=True, redactions=True, redact_images=0, remove_links=True, reset_fields=True,
              reset_responses=True, thumbnails=True, xml_metadata=True)
    for page in doc:
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
        for w in list(page.widgets() or []):
            page.delete_widget(w)
        for link in page.get_links():
            page.delete_link(link)
        for k in ("Annots", "AA", "Thumb", "PieceInfo", "Metadata", "B", "StructParents"):
            if doc.xref_get_key(page.xref, k)[0] != "null":
                doc.xref_set_key(page.xref, k, "null")
    doc.del_xml_metadata()
    doc.set_toc([])
    cat = doc.pdf_catalog()
    for k in ("Metadata", "Outlines", "AcroForm", "OpenAction", "AA", "Names", "Dests", "Info", "StructTreeRoot",
              "MarkInfo", "PieceInfo", "Threads", "SpiderInfo", "URI", "PageLabels", "Collection", "Perms", "Legal",
              "Lang") + (() if keep_optional_content else ("OCProperties",)):
        if doc.xref_get_key(cat, k)[0] != "null":
            doc.xref_set_key(cat, k, "null")
    if doc.xref_get_key(-1, "Info")[0] != "null":
        doc.xref_set_key(-1, "Info", "null")


def save_clean(doc, path, garbage=4, clean=True):
    doc.save(path, garbage=garbage, clean=clean, deflate=True, incremental=False,
             encryption=pymupdf.PDF_ENCRYPT_NONE)


def redact_pdf(src: str, out: str, matchers, mode: str, auto_surnames: bool = True, barcodes: bool = False,
               address: bool = False) -> dict:
    t0 = time.perf_counter()
    timings = {}
    lap_t = [t0]

    def lap(name):
        now = time.perf_counter()
        timings[name] = round((now - lap_t[0]) * 1000)
        lap_t[0] = now

    doc = open_pdf(src)
    n_pages = doc.page_count
    hints: list = []
    auto: list | None = [] if auto_surnames else None
    images: dict = {}
    addr_texts: list = []
    rects, counts, ocr_pages, heavy_pages = find_hits(doc, matchers, hints, auto, barcodes, images, address, addr_texts)
    # targets of the leftover image scan, taken BEFORE redaction removes the label values from the text layer
    terms = [lb for lb, _ in matchers if not lb.startswith(("label:", "regex:", "builtin:"))]
    targets = scan_targets(terms, [v for _, v in text_layer_values(doc, matchers)]
                           + [v for info in images.values() for v in info.get("values", [])])
    # verify compares in VISIBLE page space (gs may bake /Rotate into the content)
    visible = {i: [pymupdf.Rect(h.core) * doc[i].rotation_matrix for h in hs] for i, hs in rects.items()}
    image_areas = {i: [pymupdf.Rect(r) * doc[i].rotation_matrix for r in v["regions"] if r not in v["barcodes"]]
                   for i, v in images.items() if v["regions"]}
    if n_pages and len(ocr_pages) == n_pages and not have_tesseract():
        raise RedactError(f"{src} has no text layer on any page and tesseract is not installed")
    lap("find")
    work = tempfile.mkdtemp(prefix="redact-")
    try:
        # Step A: two-pass true redaction, in unrotated page space.
        for i, rs in rects.items():
            page = doc[i]
            rot = page.rotation
            if rot:
                page.set_rotation(0)
            for h in rs:
                page.add_redact_annot(h.rect, fill=(0, 0, 0), cross_out=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                                  text=pymupdf.PDF_REDACT_TEXT_NONE)
            for h in rs:
                band = text_band(h.rect, h.kind)
                if not band.is_empty:
                    page.add_redact_annot(band, fill=(0, 0, 0), cross_out=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            if rot:
                page.set_rotation(rot)
        lap("redact")

        # Step A2: leftover scan. Faint, rotated or mirrored text in images (ticket security backgrounds, big grey
        # order numbers) is not reliably found word by word. Every image that is not blank yet is read again in
        # several variants; if any piece of a target is still readable, the whole image is removed (pixels only:
        # text drawn over it stays) and the spot is outlined.
        removed: dict[int, list] = {}
        n_extra: dict[int, int] = {}
        rots = {i: doc[i].rotation for i in images}
        for i, r in rots.items():
            if r:
                doc[i].set_rotation(0)
        small_items, big_jobs = [], []
        if targets:
            for i, info in images.items():
                page = doc[i]
                for r in info["regions"]:
                    if r in info["barcodes"] or is_blank(page, r):  # barcodes: only via --barcodes
                        continue
                    if is_big(page, r):
                        big_jobs += [((i, len(big_jobs), t), page, r, rots[i], t) for t in (None, 200, 235)]
                    elif r in info["hit_regions"] or (info["page_has_hits"] and r in info["data_regions"]):
                        # the image demonstrably holds a target, or it repeats this page's data while the page has
                        # targets: faint copies may remain that no OCR variant reads -> remove it without scanning
                        removed.setdefault(i, []).append(r)
                    else:
                        small_items.append((page, r, rots[i]))
        hits = scan_regions_multi(small_items, targets) if small_items else []
        # page-sized images (scans): never wipe the page; black out every OCR word (plain + high contrast) that
        # still contains a piece of a target
        # same patterns as the main search (whole terms, labels, values), on high-contrast OCR variants
        words_left: dict[int, list] = defaultdict(list)
        all_vals = [v for _, v in text_layer_values(doc, matchers)] + \
                   [v for info in images.values() for v in info.get("values", [])]
        pats = [rx for _, rx in matchers] + [value_regex(v) for v in dict.fromkeys(all_vals)]
        for (i, n, _), ws in ocr_jobs_multi(big_jobs).items():
            page = doc[i]
            it = ImageText(page, regions=[big_jobs[n][2]], prefetched={0: ws}, barcodes=[])
            for rx in pats:
                for m in rx.finditer(it.text):
                    a_, b_ = match_span(m)
                    if b_ > a_:
                        words_left[i].extend(h.core for h in it.rects(a_, b_))
        for i, wl in words_left.items():
            page = doc[i]
            for wr in wl:
                page.add_redact_annot(wr + (-1, -1, 1, 1), fill=(0, 0, 0), cross_out=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            n_extra[i] = len(wl)
        for (page, r, _), h in zip(small_items, hits):
            if h:
                removed.setdefault(page.number, []).append(r)
        for i, hit in removed.items():
            page = doc[i]
            for r in hit:
                page.add_redact_annot(r, fill=None, cross_out=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_NONE)
            for r in hit:
                page.draw_rect(r, color=(0.55, 0.55, 0.55), width=0.6, dashes="[3 2] 0")
        for i, r in rots.items():
            if r:
                doc[i].set_rotation(r)
        lap("image_scan")

        sanitize(doc, keep_optional_content=True)  # OC kept so gs drops hidden layers like the viewer does
        step_a = os.path.join(work, "stepA.pdf")
        save_clean(doc, step_a, garbage=3)
        doc.close()

        before_gs = step_a
        if mode == "flatten":
            pattern = os.path.join(work, "p_%05d.png")
            run_gs(["-sDEVICE=png16m", f"-r{FLATTEN_DPI}", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
                    "-dUseCropBox", "-dNumRenderingThreads=4", "-o", pattern, step_a], None)
            src_doc = pymupdf.open(step_a)
            flat = pymupdf.open()
            for i, sp in enumerate(src_doc):
                png = os.path.join(work, f"p_{i + 1:05d}.png")
                if not os.path.isfile(png):
                    raise RedactError(f"Ghostscript produced no image for page {i + 1}")
                r = sp.rect  # visible area, rotation applied = what gs rendered
                np_ = flat.new_page(width=r.width, height=r.height)
                np_.insert_image(pymupdf.Rect(0, 0, r.width, r.height), filename=png, keep_proportion=False)
                os.remove(png)
            src_doc.close()
            before_gs = os.path.join(work, "flat.pdf")
            save_clean(flat, before_gs, garbage=1, clean=False)
            flat.close()
            lap("flatten")

        gs_out = os.path.join(work, "gs.pdf")
        gs_args = ["-sDEVICE=pdfwrite", "-dPDFSETTINGS=/printer", "-dAutoRotatePages=/None",
                   "-dPreserveAnnots=false", "-dNumRenderingThreads=4"]
        if mode == "flatten":
            gs_args += ["-dAutoFilterColorImages=false", "-dColorImageFilter=/FlateEncode",
                        "-dAutoFilterGrayImages=false", "-dGrayImageFilter=/FlateEncode",
                        "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false"]
        run_gs(gs_args + ["-o", gs_out, before_gs], gs_out)
        lap("gs_rewrite")

        final = pymupdf.open(gs_out)
        sanitize(final)
        tmp = out + ".tmp"
        save_clean(final, tmp, garbage=4)
        final.close()
        os.replace(tmp, out)
        write_sidecar(out, src, mode, n_pages, visible, image_areas)
        lap("sanitize")
    finally:
        shutil.rmtree(work, ignore_errors=True)  # intermediates hold unflattened content
        if os.path.exists(out + ".tmp"):
            os.remove(out + ".tmp")
    timings["total"] = round((time.perf_counter() - t0) * 1000)

    per_term: dict[str, dict] = {}
    for label in [lb for lb, _ in matchers] + (["barcodes"] if barcodes else []) + (["address"] if address else []):
        pages = {p: c for (lb, p), c in sorted(counts.items()) if lb == label}
        per_term[label] = {"total": sum(pages.values()), "pages": pages}
    return {"input": src, "output": out, "mode": mode, "pages": n_pages,
            "redactions": sum(counts.values()), "areas": sum(len(v) for v in rects.values()),
            "per_term": per_term, "not_found": [lb for lb, v in per_term.items() if v["total"] == 0],
            "needs_ocr_pages": ocr_pages, "image_heavy_pages": heavy_pages, "partial_leftovers": hints,
            "auto_added": auto or [], "timings_ms": timings,
            "image_hits": sum(v["hits"] for v in images.values()),
            "images_removed": {i + 1: len(v) for i, v in removed.items()},
            "scan_extra_words": {i + 1: n for i, n in n_extra.items()},
            "address_texts": addr_texts,
            "images_ocr": any(v["ocr"] for v in images.values()),
            "unredacted_barcode_pages": [] if barcodes else [i + 1 for i, v in images.items() if v["barcodes"]]}


def write_sidecar(out: str, src: str, mode: str, n_pages: int, rects: dict, image_areas: dict | None = None):
    data = {"version": 1, "source": os.path.abspath(src), "mode": mode, "pages": n_pages,
            "rects": {str(i + 1): [[round(v, 2) for v in r] for r in rs] for i, rs in rects.items()},
            "image_areas": {str(i + 1): [[round(v, 2) for v in r] for r in rs] for i, rs in (image_areas or {}).items()}}
    with open(out + SIDECAR_EXT, "w") as f:
        json.dump(data, f)


def default_out(src: str) -> str:
    p = Path(src)
    return str(p.with_name(p.stem + "_redacted.pdf"))


def load_targets(src: str) -> dict:
    try:
        with open(src + TARGETS_EXT) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cmd_apply(a) -> int:
    if a.more:
        # add to the targets of the previous apply on this file (a follow-up stage needs only the new targets)
        prev = load_targets(a.file)
        if not prev:
            raise RedactError(f"--more: no earlier apply on {a.file}; give all targets")
        for key in ("terms", "regex", "builtin", "label"):
            setattr(a, key, list(dict.fromkeys(prev.get(key, []) + (getattr(a, key) or []))))
        a.barcodes = a.barcodes or prev.get("barcodes", False)
        if a.address is None:
            a.address = prev.get("address", False)
        a.mode = a.mode or prev.get("mode")
    a.mode = a.mode or "flatten"
    if isinstance(a.address, list):  # --address [TEXT ...]: flag + optional exact address texts
        a.terms = list(dict.fromkeys((a.terms or []) + a.address))
        a.address = True
    a.address = bool(a.address)
    matchers = build_matchers(a.terms, a.regex, a.builtin, a.label)
    out = a.output or default_out(a.file)
    if os.path.abspath(out) == os.path.abspath(a.file):
        raise RedactError("output must differ from input")
    res = redact_pdf(a.file, out, matchers, a.mode, auto_surnames=not a.exact, barcodes=a.barcodes,
                     address=a.address)
    addr = [t for t in dict.fromkeys(res.pop("address_texts")) if len(t.strip()) >= 4]
    ver = verify_pdf(out, (a.terms or []) + addr, a.regex, a.builtin, a.mode, res["pages"], original=a.file,
                     labels=a.label, quick_images=True)
    write_verified_marker(out, ver["passed"])
    res["verify"] = ver
    try:  # best effort (read-only input folder): only --more needs it
        with open(a.file + TARGETS_EXT, "w") as f:
            json.dump({"terms": a.terms or [], "regex": a.regex or [], "builtin": a.builtin or [],
                       "label": a.label or [], "barcodes": a.barcodes, "address": a.address, "mode": a.mode}, f)
    except OSError:
        pass
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
        return 0 if ver["passed"] else 1
    print(f"Redacted {res['redactions']} occurrence(s) ({res['areas']} area(s)) on {res['pages']} page(s), "
          f"mode={res['mode']}, {res['timings_ms']['total']} ms")
    for label, v in res["per_term"].items():
        where = ", ".join(f"p{p}: {c}" for p, c in v["pages"].items()) or "-"
        print(f"  {label}: {v['total']}  ({where})")
    if res["image_hits"]:
        print(f"  ({res['image_hits']} of these found by OCR inside images and blacked out there)")
    if res["images_removed"]:
        where = ", ".join(f"p{p}: {n}" for p, n in res["images_removed"].items())
        print(f"  image(s) removed completely (outlined in the output) because they contain redacted data, also as "
              f"faint/rotated/mirrored copies (e.g. ticket security background, grey order number): {where}")
    if res["unredacted_barcode_pages"]:
        print(f"  barcode/QR code(s) kept on page(s) {', '.join(map(str, res['unredacted_barcode_pages']))} "
              f"(only black them out with --barcodes if the user asks for it)")
    if res["not_found"]:
        print(f"WARNING: not found: {', '.join(res['not_found'])}")
    ocr_like = sorted(set(res["needs_ocr_pages"] + res["image_heavy_pages"]))
    if ocr_like:
        print(f"NOTE: page(s) {', '.join(map(str, ocr_like))} were searched by OCR (scan or mostly images); OCR can "
              f"miss unusual fonts/handwriting - tell the user to look over those pages."
              + (f" Full app for difficult scans: {FULL_APP}" if FULL_APP else ""))
    if res.get("scan_extra_words"):
        print(f"  leftover scan blacked out {sum(res['scan_extra_words'].values())} more OCR word(s) on page images")
    for h in res["auto_added"]:
        print(f"  also redacted '{h['word']}' standing alone after a title on page {h['page']} "
              f"(same person as '{h['from_term']}')")
    if res["partial_leftovers"]:
        for h in res["partial_leftovers"]:
            ctx = " | ".join(f"...{c}..." for c in h["contexts"])
            print(f"CHECK: '{h['word']}' (part of '{h['from_term']}') still appears {h['count']}x alone on page "
                  f"{h['page']}, NOT redacted: {ctx}  -> only if that is the same person, re-run apply on the "
                  f"ORIGINAL file with --terms \"{h['word']}\" added.")
    all_t = (len(a.terms or []) + len(a.label or []) + len(split_builtins(a.builtin)) + len(a.regex or []))
    print(f"Targets now ({all_t}, remembered for --more): {len(a.terms or [])} term(s), labels {a.label or '-'}, "
          f"builtins {split_builtins(a.builtin) or '-'}" + (", address" if a.address else "")
          + (", barcodes" if a.barcodes else ""))
    print(f"Output: {res['output']}")
    failed = [c for c in ver["checks"] if not c["passed"]]
    for c in failed:
        print(f"[FAIL] {c['check']}: {c['detail']}")
    if failed:
        print(f"VERIFY FAILED ({ver['ms']} ms): do NOT hand out this file; tell the user what failed.")
        return 1
    print(f"VERIFY PASSED ({ver['ms']} ms, {len(ver['checks'])} checks; verification is built into apply). The file "
          f"is attached to the chat automatically. In your reply give only counts per category and pages - do not "
          f"repeat the redacted values.")
    return 0


# --------------------------------------------------------------------------- verify

def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s.translate(INVISIBLE))
    return re.sub(r"\s+", " ", s).strip().lower()


def needle_bytes(terms) -> list[tuple[str, bytes]]:
    """(term, encoded variant): Latin-1, UTF-8, UTF-16BE of term / lower / upper / title."""
    out = []
    seen = set()
    for t in terms:
        t = normalize(t)
        if len(t) < 2:
            continue
        for v in {t, t.upper(), t.title(), t.capitalize()}:
            for enc in ("latin-1", "utf-8", "utf-16-be"):
                try:
                    b = v.encode(enc)
                except UnicodeEncodeError:
                    continue
                if b not in seen:
                    seen.add(b)
                    out.append((t, b))
    return out


class ByteScanner:
    def __init__(self, terms):
        self.needles = needle_bytes(terms)
        # ASCII-case-insensitive byte regex per needle; non-ASCII case variants come from needle_bytes.
        self.rx = [(t, re.compile(re.escape(b), re.IGNORECASE)) for t, b in self.needles]

    def scan(self, data: bytes) -> set[str]:
        return {t for t, rx in self.rx if rx.search(data)}


FORBIDDEN_KEYS = ("/Outlines", "/AcroForm", "/EmbeddedFiles", "/JavaScript", "/JS", "/OpenAction", "/Annots", "/AA")


def walk_strings(v, out: list):
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        for k, x in v.items():
            out.append(k)
            walk_strings(x, out)
    elif isinstance(v, list):
        for x in v:
            walk_strings(x, out)


def walk_keys(v, out: set):
    if isinstance(v, dict):
        for k, x in v.items():
            if k.startswith("/"):
                out.add(k)
            walk_keys(x, out)
    elif isinstance(v, list):
        for x in v:
            walk_keys(x, out)


def raw_scan(pdf: str, terms: list[str], work: str) -> tuple[list[str], list[str], str | None]:
    """qpdf JSON v2: decoded strings + every decoded non-image stream + the file bytes.
    -> (leaks, forbidden structures, error). The object list is read without stream data; non-image streams are
    then decoded one by one in memory (decoding every page image to disk would need hundreds of MB)."""
    jpath = os.path.join(work, "q.json")
    r = run(["qpdf", "--json=2", "--json-stream-data=none", pdf, jpath], timeout=120)
    if r.returncode not in (0, 3) or not os.path.isfile(jpath):
        return [], [], f"qpdf json failed (exit {r.returncode}): {r.stderr[-300:]}"
    with open(jpath, encoding="utf-8") as f:
        q = json.load(f)
    objs = q["qpdf"][1]
    scanner = ByteScanner(terms)
    needles_u = sorted({normalize(t) for t in terms if len(normalize(t)) >= 2})
    leaks, structure = set(), set()
    for oid, o in objs.items():
        keys: set = set()
        walk_keys(o, keys)
        for k in FORBIDDEN_KEYS:
            if k in keys:
                structure.add(f"{oid}: {k}")
        # decoded strings ("u:" unicode, "b:" hex bytes) and names
        strs: list = []
        walk_strings(o.get("value", o.get("stream", {}).get("dict", {})), strs)
        if "stream" in o:
            walk_strings(o["stream"].get("dict", {}), strs)
        for s in strs:
            if s.startswith("u:"):
                txt = normalize(s[2:])
                for n in needles_u:
                    if n in txt:
                        leaks.add(f"{oid} string: '{n}'")
            elif s.startswith("b:"):
                try:
                    for t in scanner.scan(bytes.fromhex(s[2:])):
                        leaks.add(f"{oid} bytes: '{t}'")
                except ValueError:
                    pass
            else:
                txt = s.lower()
                for n in needles_u:
                    if n in txt:
                        leaks.add(f"{oid} name/string: '{n}'")
        st = o.get("stream")
        if st is not None:
            d = st.get("dict", {})
            is_image = d.get("/Subtype") == "/Image" or d.get("/Type") == "/XObject" and d.get("/Subtype") == "/Image"
            if not is_image:
                num = re.match(r"obj:(\d+) (\d+) R", oid)
                cp = subprocess.run(["qpdf", f"--show-object={num.group(1)},{num.group(2)}", "--filtered-stream-data",
                                     "--decode-level=generalized", pdf], capture_output=True, timeout=120) if num else None
                if cp is None or cp.returncode not in (0, 3):
                    return sorted(leaks), sorted(structure), f"stream {oid} could not be decoded"
                for t in scanner.scan(cp.stdout):
                    leaks.add(f"{oid} stream: '{t}'")
    with open(pdf, "rb") as fh:
        for t in scanner.scan(fh.read()):
            leaks.add(f"file bytes: '{t}'")
    return sorted(leaks), sorted(structure), None


def residual_check(pdf: str, side: dict) -> list[str]:
    """Any non-space glyph whose bbox centre lies inside a redaction rect = leftover (e.g. glyph ends)."""
    problems = []
    doc = pymupdf.open(pdf)
    try:
        for pno, rs in side.get("rects", {}).items():
            i = int(pno) - 1
            if i >= doc.page_count:
                problems.append(f"page {pno} missing")
                continue
            page = doc[i]
            mat = page.rotation_matrix  # unrotated -> visible space, like the rects
            if page.rotation:
                page.set_rotation(0)  # in memory only
            boxes = [pymupdf.Rect(r) for r in rs]
            n = 0
            for c, x, y in char_centres(page):
                pt = pymupdf.Point(x, y) * mat
                if any(b.x0 <= pt.x <= b.x1 and b.y0 <= pt.y <= b.y1 for b in boxes):
                    n += 1
            if n:
                problems.append(f"page {pno}: {n} glyph(s) left inside redaction areas")
    finally:
        doc.close()
    return problems


def verify_pdf(pdf: str, terms: list[str], regexes, builtins, mode: str, expect_pages: int | None,
               rects_path: str | None = None, original: str | None = None, labels=None,
               quick_images: bool = False) -> dict:
    """quick_images: one OCR pass per image area instead of all SCAN_VARIANTS (apply already ran the full scan on
    the redacted content right before flattening)."""
    t0 = time.perf_counter()
    checks = []
    side = None
    side_err = None
    rects_path = rects_path or pdf + SIDECAR_EXT
    if os.path.isfile(rects_path):
        try:
            with open(rects_path) as f:
                side = json.load(f)
        except (OSError, ValueError) as e:
            side_err = f"cannot read {rects_path}: {e}"
    original = original or (side or {}).get("source")
    values, values_err = [], None
    if regexes or split_builtins(builtins) or labels:
        if original and os.path.isfile(original):
            try:
                values = matched_values(original, regexes, builtins, labels)
            except (RedactError, re.error) as e:
                values_err = f"cannot read original {original}: {e}"
        else:
            values_err = (f"original file not found ({original or 'unknown'}); pass --original FILE.pdf so the "
                          f"matched --regex/--builtin values can be scanned for")

    def add(name, problems, ok_msg):
        checks.append({"check": name, "passed": not problems, "detail": ok_msg if not problems else "; ".join(problems[:20])})

    # 1. open + page count + qpdf --check
    problems, texts, meta, structure = [], None, [], []
    try:
        doc = pymupdf.open(pdf)
        if doc.needs_pass:
            problems.append("password required")
        if expect_pages is not None and doc.page_count != expect_pages:
            problems.append(f"page count {doc.page_count} != {expect_pages}")
        texts = [p.get_text("text") for p in doc]
        # metadata (do not call doc.metadata: it may materialise an /Info dict)
        it, iv = doc.xref_get_key(-1, "Info")
        if it == "xref":
            x = int(iv.split()[0])
            for k in doc.xref_get_keys(x):
                kt, kv = doc.xref_get_key(x, k)
                if not (kt == "null" or (kt == "string" and kv == "")):
                    meta.append(f"Info /{k} present")
        elif it == "dict" and not re.fullmatch(r"\s*<<\s*>>\s*", iv):
            meta.append("Info dict not empty")
        cat = doc.pdf_catalog()
        if doc.xref_get_key(cat, "Metadata")[0] != "null":
            meta.append("XMP /Metadata present")
        for k in ("Outlines", "AcroForm", "OpenAction", "AA", "Names/EmbeddedFiles", "Names/JavaScript"):
            if doc.xref_get_key(cat, k)[0] != "null":
                structure.append(f"catalog /{k}")
        for i in range(doc.page_count):
            for k in ("Annots", "AA"):
                if doc.xref_get_key(doc.page_xref(i), k)[0] != "null":
                    structure.append(f"page {i + 1} /{k}")
        n_pages = doc.page_count
        doc.close()
    except Exception as e:
        problems.append(f"cannot open: {e}")
        n_pages = 0
    qc = run(["qpdf", "--check", pdf], timeout=120)
    if qc.returncode != 0:
        problems.append(f"qpdf --check exit {qc.returncode}: {(qc.stderr + qc.stdout)[-200:].strip()}")
    add("open", problems, f"opens, {n_pages} page(s), qpdf --check ok")

    # 2. text leak
    problems = []
    needles = sorted({normalize(t) for t in list(terms) + values if len(normalize(t)) >= 2})
    vset = set(values) - {normalize(t) for t in terms}
    pats = [term_regex(t) for t in terms if t.strip()]
    pats += [re.compile(r) for r in regexes or []]
    pats += [re.compile(BUILTINS[b]) for b in split_builtins(builtins)]
    if texts is None:
        problems.append("text not extractable")
    else:
        for i, t in enumerate(texts):
            nt = normalize(t)
            if mode == "flatten" and nt:
                problems.append(f"page {i + 1}: flatten output has extractable text ({len(nt)} chars)")
            for n in needles:
                if n in nt:
                    problems.append(f"page {i + 1}: contains " + ("a matched value" if n in vset else f"'{n}'"))
            raw_t = t.translate(INVISIBLE)
            for p in pats:
                if p.search(raw_t):
                    problems.append(f"page {i + 1}: pattern {p.pattern[:40]!r} still matches")
    add("text-leak", sorted(set(problems)),
        "no extractable text" if mode == "flatten" else "no redacted string/pattern in extracted text")

    # 2b. residual glyphs inside the redaction areas (partial leftovers such as glyph ends)
    if side_err:
        problems = [side_err]
    elif side is None:
        problems = ([] if mode == "flatten" else
                    [f"redaction areas file {rects_path} missing (written by apply); re-run apply"])
    else:
        problems = residual_check(pdf, side)
    add("residual", problems, "no glyph inside any redaction area")

    # 2c. text inside (former) image areas, OCR of the output (faint/rotated/mirrored variants unless quick)
    areas = (side or {}).get("image_areas") or {}
    targets = scan_targets(terms, values)
    targets_full = scan_targets(terms, values, pieces=False)  # page images (scans): whole names/values, like text
    if areas and targets and have_tesseract():
        problems = []
        try:
            out_doc = pymupdf.open(pdf)
            by_page, rots = {}, {}
            for pg, rs in areas.items():
                i = int(pg) - 1
                page = out_doc[i]
                rots[i] = page.rotation
                if rots[i]:
                    page.set_rotation(0)
                regions = [pymupdf.Rect(r) * page.derotation_matrix for r in rs] if rots[i] else [pymupdf.Rect(r) for r in rs]
                by_page[i] = [r for r in regions if not is_blank(page, r)]
                if rots[i]:
                    page.set_rotation(rots[i])
            by_page = {i: rs for i, rs in by_page.items() if rs}
            if quick_images:
                for i, it in image_texts(out_doc, by_page, detect_barcodes=False).items():
                    per_region: dict[int, str] = defaultdict(str)
                    for ln, k in enumerate(it.line_region):
                        per_region[k] += "|" + "".join(c for c, l in zip(it.text, it.lines) if l == ln)
                    for k, txt in per_region.items():
                        tg = targets_full if is_big(out_doc[i], it.regions[k]) else targets
                        if any(x in compact(txt) for x in tg):
                            problems.append(f"page {i + 1}: target piece(s) readable inside an image (OCR)")
            else:
                for i, r in rots.items():
                    if r:
                        out_doc[i].set_rotation(0)
                items = [(out_doc[i], r, rots[i]) for i, rs in by_page.items() for r in rs if not is_big(out_doc[i], r)]
                big_items = [(out_doc[i], r, rots[i]) for i, rs in by_page.items() for r in rs if is_big(out_doc[i], r)]
                res_ = (scan_regions_multi(items, targets) if items else []) + \
                       (scan_regions_multi(big_items, targets_full) if big_items else [])
                for (page, _, _), h in zip(items + big_items, res_):
                    if h:
                        problems.append(f"page {page.number + 1}: target piece(s) readable inside an image (OCR)")
            out_doc.close()
        except Exception as e:
            problems.append(f"image OCR failed: {e}")
        add("image-ocr", sorted(set(problems)),
            f"no target readable by OCR in {sum(map(len, areas.values()))} image area(s)"
            + ("" if quick_images else " (incl. faint, rotated and mirrored text)"))

    # 3. raw leak + structure scan via qpdf
    work = tempfile.mkdtemp(prefix="verify-")
    try:
        leaks, qstruct, err = raw_scan(pdf, list(terms) + values, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    leaks = [re.sub(r"'.*'$", "'<value>'", x) if any(normalize(v) in x for v in values) else x for x in leaks]
    add("raw-leak", leaks + ([err] if err else []) + ([values_err] if values_err else []),
        f"no term{' or matched value (' + str(len(values)) + ')' if values else ''} in decoded strings, "
        f"decoded non-image streams or file bytes (Latin-1/UTF-8/UTF-16BE)")

    # 4. metadata
    add("metadata", meta, "no Info entries, no XMP")

    # 5. structure
    add("structure", sorted(set(structure + qstruct)),
        "no outlines, forms, embedded files, JavaScript, OpenAction or annotations")

    # 6. single revision
    with open(pdf, "rb") as fh:
        data = fh.read()
    eofs = data.count(b"%%EOF")
    prev = re.search(rb"/Prev[\s\0/\[<(0-9]", data) is not None
    add("single-revision", ([] if eofs == 1 else [f"%%EOF count {eofs}"]) + (["/Prev present"] if prev else []),
        "one %%EOF, no /Prev")

    return {"file": pdf, "passed": all(c["passed"] for c in checks), "checks": checks,
            "ms": round((time.perf_counter() - t0) * 1000)}


def write_verified_marker(pdf: str, passed: bool):
    marker = pdf + VERIFIED_EXT
    if passed:
        with open(pdf, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        with open(marker, "w") as f:
            f.write(digest + "\n")
    elif os.path.exists(marker):
        os.remove(marker)


def cmd_verify(a) -> int:
    a.mode = a.mode or "flatten"
    for b in split_builtins(a.builtin):
        if b not in BUILTINS:
            raise RedactError(f"unknown builtin {b!r}")
    if not os.path.isfile(a.file):
        raise RedactError(f"file not found: {a.file}")
    res = verify_pdf(a.file, a.terms or [], a.regex, a.builtin, a.mode, a.expect_pages, a.rects, a.original,
                     a.label)
    write_verified_marker(a.file, res["passed"])
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        for c in res["checks"]:
            print(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['check']}: {c['detail']}")
        print(f"VERIFY {'PASSED' if res['passed'] else 'FAILED'} ({res['ms']} ms): {res['file']}")
    return 0 if res["passed"] else 1


def cmd_builtins(a) -> int:
    for k in BUILTINS:
        print(k)
    return 0


# --------------------------------------------------------------------------- CLI

def retention_minutes() -> int:
    """REDACT_RETENTION_MIN or ~/.config/redact/retention_min; 0 = off (default)."""
    v = os.environ.get("REDACT_RETENTION_MIN")
    if v is None:
        try:
            with open(os.path.expanduser("~/.config/redact/retention_min")) as f:
                v = f.read().strip()
        except OSError:
            v = "0"
    try:
        return max(0, int(v))
    except ValueError:
        return 0


def sweep_old_files():
    """Privacy: delete PDFs and redact side files in the home directory (top level only, where chat uploads land)
    older than the retention time. Runs at the start of every redact command; off unless configured."""
    minutes = retention_minutes()
    if not minutes:
        return
    cutoff = time.time() - minutes * 60
    home = Path.home()
    for f in home.iterdir():
        name = f.name.lower()
        if f.is_file() and (name.endswith(".pdf") or name.endswith(SIDECAR_EXT) or name.endswith(VERIFIED_EXT)
                            or name.endswith(TARGETS_EXT)):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="redact", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="page texts + per-page flags (no_text / needs_ocr)")
    p.add_argument("file")
    p.add_argument("--pages", help="only these pages, e.g. 3 or 2-5 or 1,4-6")
    p.add_argument("--full", action="store_true", help="no de-duplication, no output limit")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_extract)

    def targets(p):
        p.add_argument("--terms", "--term", action="extend", nargs="+", default=[],
                       help="literal text (repeatable; several values allowed)")
        p.add_argument("--label", "--labels", action="extend", nargs="+", default=[],
                       help="redact the value after this label on the same line, e.g. \"Auftragsnummer\" (repeatable)")
        p.add_argument("--regex", action="extend", nargs="+", default=[], help="Python regex (repeatable)")
        p.add_argument("--builtin", "--builtins", action="extend", nargs="+", default=[],
                       help=f"one of {', '.join(BUILTINS)} (repeatable)")
        p.add_argument("--mode", choices=("flatten", "text"), default=None, help="default flatten")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("apply", help="redact, flatten/rewrite, scrub")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    p.add_argument("--more", action="store_true",
                   help="follow-up stage: keep all targets of the previous apply on this file and add the given ones")
    p.add_argument("--address", nargs="*", default=None, metavar="TEXT",
                   help="also redact the postal address of each --terms name (block below the name or a labelled "
                        "'Anschrift:' field); optional address texts are redacted as given")
    p.add_argument("--barcodes", action="store_true",
                   help="also black out every barcode / QR code (they can encode names, e.g. on tickets)")
    p.add_argument("--exact", action="store_true",
                   help="do not auto-redact a surname standing alone after Herr/Frau/Dr./Mr. ...")
    targets(p)
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("verify", help="hard checks on a redacted PDF (exit 1 on failure)")
    p.add_argument("file")
    p.add_argument("--expect-pages", type=int)
    p.add_argument("--rects", help=f"redaction areas file from apply (default: FILE{SIDECAR_EXT})")
    p.add_argument("--original", help="original PDF, to scan for the values --regex/--builtin matched "
                                      "(default: recorded by apply)")
    targets(p)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("builtins", help="list builtin patterns")
    p.set_defaults(fn=cmd_builtins)

    a = ap.parse_args(argv)
    sweep_old_files()
    try:
        return a.fn(a)
    except RedactError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
