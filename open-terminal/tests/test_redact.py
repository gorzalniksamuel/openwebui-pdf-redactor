"""Tests for the lean `redact` CLI. Run inside the image:
    REDACT_FIXTURES=<dir with test PDFs> python3 -m pytest -q tests   (needs pymupdf, pytest, gs, qpdf, tesseract)
PDFs are generated in-test; the repo fixtures (digital ones) are used when mounted at $REDACT_FIXTURES (/fx).
"""
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import redact  # noqa: E402

FX = Path(os.environ.get("REDACT_FIXTURES", "/fx"))
REDACT = [sys.executable, str(Path(redact.__file__).resolve())]

LETTER_LINES = [
    "Schmidt & Weber Consulting GmbH, Berliner Straße 5, 10115 Berlin",
    "Herrn Dr. Thomas Müller, Musterweg 12, 80331 München",
    "Sehr geehrter Herr Dr. Müller,",
    "wie mit Herrn Max Weber besprochen, übernimmt Erika Mustermann die Betreuung.",
    "Bitte überweisen Sie auf die IBAN DE89 3704 0044 0532 0130 00 bis zum 12.03.2026.",
    "Rückfragen: +49 171 2345678 oder erika.mustermann@example.de",
    "Maximal zehn Tage Frist. Mit freundlichen Grüßen",
]


def run(*args, check=False):
    r = subprocess.run([*REDACT, *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{args}: exit {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def make_letter(path, leading=14, extras=False):
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for ln in LETTER_LINES:
        page.insert_text((60, y), ln, fontsize=11, fontname="helv")
        y += leading
    if extras:
        doc.set_metadata({"title": "Brief an Thomas Müller", "author": "Erika Mustermann", "subject": "IBAN"})
        doc.set_xml_metadata("<x:xmpmeta xmlns:x='adobe:ns:meta/'>Thomas Müller</x:xmpmeta>")
        page.add_text_annot((400, 400), "Notiz: Max Weber anrufen")
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(60, 60, 200, 80),
                          "uri": "mailto:erika.mustermann@example.de"})
        doc.set_toc([[1, "Thomas Müller", 1]])
        doc.embfile_add("secret.txt", b"Max Weber IBAN DE89370400440532013000")
        w = pymupdf.Widget()
        w.field_name, w.field_type, w.field_value = "name", pymupdf.PDF_WIDGET_TYPE_TEXT, "Max Weber"
        w.rect = pymupdf.Rect(60, 500, 260, 520)
        page.add_widget(w)
        cat = doc.pdf_catalog()
        js = doc.get_new_xref()
        doc.update_object(js, "<< /S /JavaScript /JS (app.alert('Max Weber')) >>")
        doc.xref_set_key(cat, "OpenAction", f"{js} 0 R")
    doc.save(path)
    doc.close()
    return path


def make_invoice(path):
    doc = pymupdf.open()
    for n in (1, 2):
        page = doc.new_page()
        page.insert_text((60, 72), f"INVOICE INV-2026-0042 page {n}", fontsize=14)
        page.insert_text((60, 100), "Billing contact: Sarah Connor <sarah.connor@example.com>", fontsize=11)
        page.insert_text((60, 116), "Phone: (555) 123-4567, card 4111 1111 1111 1111", fontsize=11)
        page.draw_rect(pymupdf.Rect(55, 130, 400, 200), color=(0, 0, 0))  # table frame
        page.insert_text((60, 150), "Consulting 10 x 120.00 = 1200.00 EUR", fontsize=11)
    doc.save(path)
    doc.close()
    return path


def make_scanned(path):
    src = pymupdf.open()
    p = src.new_page()
    p.insert_text((60, 72), "Scanned memo Max Weber", fontsize=14)
    pix = p.get_pixmap(dpi=100)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    return path


def page_text(path):
    with pymupdf.open(path) as d:
        return "\n".join(p.get_text() for p in d)


# ------------------------------------------------------------------ unit

def test_term_regex_case_insensitive_word_boundary():
    rx = redact.term_regex("max weber")
    assert rx.search("Herrn MAX  WEBER besprochen")
    assert rx.search("Max\nWeber")
    assert not rx.search("Maximal Weberei")
    assert not redact.term_regex("Max").search("Maximal")


def test_builtins_compile_and_match():
    assert redact.BUILTINS.keys() >= {"EMAIL", "PHONE", "IBAN", "DATE"}
    import re
    assert re.search(redact.BUILTINS["IBAN"], "IBAN DE89 3704 0044 0532 0130 00.")
    assert re.search(redact.BUILTINS["EMAIL"], "x erika.mustermann@example.de y")
    assert re.search(redact.BUILTINS["DATE"], "am 12.03.2026")
    assert re.search(redact.BUILTINS["PHONE"], "+49 171 2345678")


def test_bad_inputs(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    assert run("apply", pdf).returncode == 2  # nothing to redact
    assert run("apply", pdf, "--builtin", "NOPE").returncode == 2
    assert run("apply", pdf, "--regex", "(").returncode == 2
    assert run("extract", tmp_path / "missing.pdf").returncode == 2


# ------------------------------------------------------------------ extract

def test_extract_json(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    r = run("extract", pdf, "--json", check=True)
    d = json.loads(r.stdout)
    assert d["pages"] == 1 and d["needs_ocr_pages"] == []
    assert "Max Weber" in d["page_texts"][0]["text"]


def test_extract_flags_scanned_page(tmp_path):
    pdf = make_scanned(tmp_path / "scan.pdf")
    d = json.loads(run("extract", pdf, "--json", check=True).stdout)
    assert d["needs_ocr_pages"] == [1] and d["page_texts"][0]["needs_ocr"]
    out = run("extract", pdf).stdout
    assert "SCANNED/IMAGE PAGE" in out and "Max Weber" in out  # OCR text of the page image


# ------------------------------------------------------------------ apply + verify

TERMS = ["--terms", "Thomas Müller", "--terms", "Müller", "--terms", "Max Weber", "--terms", "Erika Mustermann"]
PATS = ["--builtin", "IBAN", "--builtin", "EMAIL", "--builtin", "PHONE"]


def test_flatten_letter_with_all_extras(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf", extras=True)
    assert run("verify", pdf, *TERMS).returncode == 1  # unredacted input must fail
    out = tmp_path / "o.pdf"
    r = run("apply", pdf, *TERMS, *PATS, "-o", out, "--json", check=True)
    res = json.loads(r.stdout)
    assert res["per_term"]["Max Weber"]["total"] >= 1  # page text + form field appearance
    assert res["per_term"]["Müller"]["total"] == 2
    assert res["per_term"]["builtin:IBAN"]["total"] == 1
    assert res["not_found"] == []
    assert page_text(out).strip() == ""
    v = run("verify", out, *TERMS, *PATS, "--expect-pages", 1)
    assert v.returncode == 0, v.stdout
    assert pdf.exists() and "Max Weber" in page_text(pdf)  # original untouched


def test_text_mode_keeps_other_text_and_adjacent_lines(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf", leading=11.5)  # tight leading: glyph boxes overlap
    out = tmp_path / "t.pdf"
    run("apply", pdf, "--mode", "text", "--terms", "Max Weber", "--builtin", "IBAN", "-o", out, check=True)
    t = page_text(out)
    assert "Max Weber" not in t and "DE89" not in t
    for keep in ("Sehr geehrter Herr Dr. Müller", "übernimmt Erika Mustermann", "Rückfragen: +49 171 2345678",
                 "Schmidt & Weber Consulting", "Maximal zehn Tage"):
        assert keep in t, keep
    v = run("verify", out, "--mode", "text", "--terms", "Max Weber", "--builtin", "IBAN")
    assert v.returncode == 0, v.stdout


def test_text_mode_verify_detects_leftover(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    out = tmp_path / "t.pdf"
    run("apply", pdf, "--mode", "text", "--terms", "Max Weber", "-o", out, check=True)
    v = run("verify", out, "--mode", "text", "--terms", "Erika Mustermann")
    assert v.returncode == 1 and "text-leak" in v.stdout


def test_flatten_verify_fails_on_text_output(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    out = tmp_path / "t.pdf"
    run("apply", pdf, "--mode", "text", "--terms", "Max Weber", "-o", out, check=True)
    assert run("verify", out, "--terms", "Max Weber").returncode == 1  # default mode flatten expects no text


def test_verify_writes_marker_only_when_passed(tmp_path):
    import hashlib
    pdf = make_letter(tmp_path / "l.pdf")
    out = tmp_path / "t.pdf"
    marker = tmp_path / "t.pdf.verified"
    run("apply", pdf, "--mode", "text", "--terms", "Max Weber", "-o", out, check=True)
    assert run("verify", out, "--mode", "text", "--terms", "Max Weber").returncode == 0
    assert marker.read_text().strip() == hashlib.sha256(out.read_bytes()).hexdigest()
    assert run("verify", out, "--mode", "text", "--terms", "Erika Mustermann").returncode == 1
    assert not marker.exists()


def test_invoice_two_pages_regex_and_case(tmp_path):
    pdf = make_invoice(tmp_path / "inv.pdf")
    out = tmp_path / "inv_redacted.pdf"
    r = run("apply", pdf, "--terms", "sarah connor", "--regex", r"4111(?: 1111){3}", "--builtin", "EMAIL", check=True)
    assert "sarah connor: 2" in r.stdout and out.exists()  # default output name
    v = run("verify", out, "--terms", "Sarah Connor", "--regex", r"4111(?: 1111){3}", "--builtin", "EMAIL",
            "--expect-pages", 2)
    assert v.returncode == 0, v.stdout


def test_rotated_page(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    with pymupdf.open(pdf) as d:
        d[0].set_rotation(90)
        d.save(tmp_path / "r.pdf")
    out = tmp_path / "ro.pdf"
    run("apply", tmp_path / "r.pdf", "--mode", "text", "--terms", "Max Weber", "-o", out, check=True)
    with pymupdf.open(out) as d:
        # gs may bake /Rotate into the page; the visible orientation (landscape) must be kept
        assert d[0].rect.width > d[0].rect.height
        t = d[0].get_text()
    assert "Max Weber" not in t and "Erika Mustermann" in t
    v = run("verify", out, "--mode", "text", "--terms", "Max Weber")
    assert v.returncode == 0 and "[PASS] residual" in v.stdout, v.stdout
    # the residual check is not blind on rotated pages: a rect over text that is still there must fail
    side = json.loads(Path(str(out) + ".redact.json").read_text())
    with pymupdf.open(out) as d:
        mat = d[0].rotation_matrix
        d[0].set_rotation(0)
        r = d[0].search_for("Erika Mustermann")[0] * mat
    side["rects"]["1"].append(list(r))
    Path(str(out) + ".redact.json").write_text(json.dumps(side))
    v = run("verify", out, "--mode", "text", "--terms", "Max Weber")
    assert v.returncode == 1 and "[FAIL] residual" in v.stdout, v.stdout


@pytest.mark.skipif(not redact.have_tesseract(), reason="tesseract not installed")
def test_scanned_page_is_redacted_by_ocr(tmp_path):
    pdf = make_scanned(tmp_path / "scan.pdf")
    out = tmp_path / "scan_redacted.pdf"
    r = run("apply", pdf, "--terms", "Max Weber", "-o", out)
    assert r.returncode == 0 and "Max Weber: 1" in r.stdout and "VERIFY PASSED" in r.stdout, r.stdout
    assert "searched by OCR" in r.stdout
    assert run("verify", out, "--terms", "Max Weber", "--original", pdf).returncode == 0
    assert "Scanned memo" in run("extract", out, check=True).stdout  # rest of the scan still readable


def test_mixed_scanned_and_digital_pages(tmp_path):
    scan = pymupdf.open(make_scanned(tmp_path / "scan.pdf"))
    doc = pymupdf.open(make_letter(tmp_path / "l.pdf"))
    doc.insert_pdf(scan)
    doc.save(tmp_path / "mixed.pdf")
    r = run("apply", tmp_path / "mixed.pdf", "--terms", "Max Weber", "--json", check=True)
    res = json.loads(r.stdout)
    assert res["needs_ocr_pages"] == [2] and res["per_term"]["Max Weber"]["pages"] == {"1": 1, "2": 1}


@pytest.mark.skipif(not redact.have_tesseract(), reason="tesseract not installed")
def test_second_stage_on_redacted_output(tmp_path):
    """Multi-stage: an already redacted (flattened, image-only) PDF gets further redactions."""
    pdf = make_letter(tmp_path / "l.pdf")
    run("apply", pdf, "--terms", "Max Weber", "Erika Mustermann", "--builtin", "IBAN", "EMAIL", check=True)
    out = tmp_path / "l_redacted.pdf"
    assert run("verify", out, "--terms", "Max Weber", "Erika Mustermann", "--builtin", "IBAN", "EMAIL").returncode == 0
    r = run("apply", out, "--terms", "Thomas Müller")
    assert r.returncode == 0 and "Thomas Müller: 2" in r.stdout and "VERIFY PASSED" in r.stdout, r.stdout
    out2 = tmp_path / "l_redacted_redacted.pdf"
    ocr = run("extract", out2, check=True).stdout
    assert "Thomas Müller" not in ocr and "Max Weber" not in ocr and "Erika Mustermann" not in ocr
    assert "Schmidt" in ocr and "Consulting" in ocr


# ------------------------------------------------------------------ repo fixtures

needs_fx = pytest.mark.skipif(not (FX / "letter_de.pdf").exists(), reason="fixtures not mounted")
FX_TERMS = ["--terms", "Thomas Müller", "--terms", "Müller", "--terms", "Max Weber", "--terms", "Anna-Lena Schmidt",
            "--terms", "Jonas Becker", "--builtin", "IBAN", "--builtin", "EMAIL", "--builtin", "PHONE"]


@needs_fx
def test_fixture_letter_de_fast(tmp_path):
    out = tmp_path / "o.pdf"
    t0 = time.perf_counter()
    run("apply", FX / "letter_de.pdf", *FX_TERMS, "-o", out, check=True)
    v = run("verify", out, *FX_TERMS, "--expect-pages", 1)
    dt = time.perf_counter() - t0
    assert v.returncode == 0, v.stdout
    assert dt < 3.0, f"apply+verify took {dt:.2f}s"


@needs_fx
def test_fixture_invoice_en(tmp_path):
    out = tmp_path / "o.pdf"
    args = ["--terms", "Sarah Connor", "--terms", "John Smith", "--builtin", "EMAIL", "--builtin", "CREDIT_CARD"]
    run("apply", FX / "invoice_en.pdf", *args, "-o", out, check=True)
    assert run("verify", out, *args, "--expect-pages", 2).returncode == 0


@needs_fx
def test_fixture_incremental_old_revision_gone(tmp_path):
    # "Max Weber" exists only in the superseded first revision; the output must not carry it either.
    out = tmp_path / "o.pdf"
    run("apply", FX / "incremental.pdf", "--terms", "Karl Beispiel", "-o", out, check=True)
    v = run("verify", out, "--terms", "Karl Beispiel", "--terms", "Max Weber", "--expect-pages", 1)
    assert v.returncode == 0, v.stdout


@needs_fx
def test_fixture_scanned_needs_ocr():
    d = json.loads(run("extract", FX / "memo_scanned.pdf", "--json", check=True).stdout)
    assert d["needs_ocr_pages"] == [1]


def test_surname_after_title_auto_redacted(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    out = tmp_path / "x.pdf"
    r = run("apply", pdf, "--terms", "Thomas Müller", "--mode", "text", "-o", out, "--json", check=True)
    res = json.loads(r.stdout)
    assert [(h["word"], h["page"]) for h in res["auto_added"]] == [("Müller", 1)]  # "Herr Dr. Müller,"
    assert res["partial_leftovers"] == []
    assert "Müller" not in page_text(out)


def test_partial_name_leftover_hint(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    # "Weber" also stands alone in the company name "Schmidt & Weber": hint only, not auto-redacted
    r = run("apply", pdf, "--terms", "Max Weber", "--json", check=True)
    res = json.loads(r.stdout)
    assert [(h["word"], h["count"]) for h in res["partial_leftovers"]] == [("Weber", 1)]
    assert "Schmidt & Weber" in res["partial_leftovers"][0]["contexts"][0]
    txt = run("apply", pdf, "--terms", "Max Weber", "-o", tmp_path / "y.pdf", check=True).stdout
    assert "CHECK: 'Weber'" in txt
    r = run("apply", pdf, "--exact", "--terms", "Thomas Müller", "--json", check=True)
    assert [h["word"] for h in json.loads(r.stdout)["partial_leftovers"]] == ["Müller"]


def test_comma_separated_builtins(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    r = run("apply", pdf, "--builtin", "email,IBAN", "--json", check=True)
    pt = json.loads(r.stdout)["per_term"]
    assert pt["builtin:EMAIL"]["total"] == 1 and pt["builtin:IBAN"]["total"] == 1


def test_hint_ignores_email_local_part(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")  # "erika.mustermann@example.de" must not trigger a hint
    r = run("apply", pdf, "--terms", "Erika Mustermann", "--json", check=True)
    assert json.loads(r.stdout)["partial_leftovers"] == []


# ------------------------------------------------------------------ review round 1

def make_oriented(path, name="Maximilian Weberknecht"):
    """Page 1: vertical line (rotate=90) with the name; page 2: same name on a 45-degree line."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((60, 72), "Horizontal: Erika Mustermann wohnt hier.", fontsize=11)
    p.insert_text((300, 500), f"Vertikal {name} Ende", fontsize=12, rotate=90)
    p2 = doc.new_page()
    p2.insert_text((60, 72), "Horizontal: Erika Mustermann wohnt hier.", fontsize=11)
    pivot = pymupdf.Point(150, 400)
    p2.insert_text(pivot, f"Schraeg {name} Ende", fontsize=12, morph=(pivot, pymupdf.Matrix(-45)))
    doc.save(path)
    return path


@pytest.mark.parametrize("mode", ["text", "flatten"])
def test_vertical_and_diagonal_text_fully_removed(tmp_path, mode):
    pdf = make_oriented(tmp_path / "o.pdf")
    with pymupdf.open(pdf) as d:  # the fixture really has non-horizontal lines
        kinds = {redact.line_kind(ln["dir"]) for pg in d for b in pg.get_text("dict")["blocks"]
                 for ln in b.get("lines", [])}
    assert {"h", "v", "d"} <= kinds, kinds
    out = tmp_path / "o_out.pdf"
    r = run("apply", pdf, "--mode", mode, "--terms", "Maximilian Weberknecht", "-o", out, "--json", check=True)
    assert json.loads(r.stdout)["per_term"]["Maximilian Weberknecht"]["pages"] == {"1": 1, "2": 1}
    t = page_text(out)
    for frag in ("Maxim", "Weber", "knecht", "ilian"):
        assert frag not in t, (frag, t)
    if mode == "text":
        assert t.count("Erika Mustermann") == 2  # unrelated horizontal text kept
        with pymupdf.open(out) as d:  # no glyph of the name left (not even the 'M' or 't' ends)
            for pg, kind, rest in ((d[0], "v", "Vertikal"), (d[1], "d", "Schraeg")):
                left = "".join(s["text"] for b in pg.get_text("dict")["blocks"] for ln in b.get("lines", [])
                               if redact.line_kind(ln["dir"]) == kind for s in ln["spans"])
                got, allowed = Counter(left.replace(" ", "")), Counter(rest + "Ende")
                if kind == "v":
                    assert got == allowed, (kind, left)
                else:  # diagonal: full-rect text pass may also take neighbouring glyphs (under the black box)
                    assert not got - allowed, (kind, left)
    v = run("verify", out, "--mode", mode, "--terms", "Maximilian Weberknecht")
    assert v.returncode == 0, v.stdout


def test_text_band_inset_perpendicular_only():
    r = pymupdf.Rect(0, 0, 20, 100)
    assert redact.text_band(r, "h") == pymupdf.Rect(0, 25, 20, 75)
    assert redact.text_band(r, "v") == pymupdf.Rect(5, 0, 15, 100)
    assert redact.text_band(r, "d") == r


def test_residual_check_fails_on_partial_leftover(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    out = tmp_path / "t.pdf"
    run("apply", pdf, "--mode", "text", "--terms", "Max Weber", "-o", out, check=True)
    side_path = Path(str(out) + ".redact.json")
    side = json.loads(side_path.read_text())
    assert set(side) >= {"source", "rects"} and "Weber" not in side_path.read_text()  # coordinates only
    # simulate a redaction that left glyph ends: rect over "Erika" (still present), term list does not name it
    with pymupdf.open(out) as d:
        r = d[0].search_for("Erika")[0]
    side["rects"]["1"].append([r.x0, r.y0, r.x1, r.y1])
    side_path.write_text(json.dumps(side))
    v = run("verify", out, "--mode", "text", "--terms", "Max Weber")
    assert v.returncode == 1 and "[FAIL] residual" in v.stdout and "glyph" in v.stdout, v.stdout
    side_path.unlink()
    v = run("verify", out, "--mode", "text", "--terms", "Max Weber")
    assert v.returncode == 1 and "missing" in v.stdout


def test_verify_scans_matched_builtin_values_in_raw_objects(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    out = tmp_path / "f.pdf"
    run("apply", pdf, "--builtin", "IBAN", "-o", out, check=True)
    assert run("verify", out, "--builtin", "IBAN").returncode == 0
    # plant the IBAN value in an unreferenced-looking object of the output (full rewrite, single revision)
    with pymupdf.open(out) as d:
        x = d.get_new_xref()
        d.update_object(x, "<< /Leak (DE89 3704 0044 0532 0130 00) >>")
        d.xref_set_key(d.pdf_catalog(), "Leak", f"{x} 0 R")
        d.save(tmp_path / "leak.pdf", garbage=0)
    os.replace(str(out) + ".redact.json", str(tmp_path / "leak.pdf") + ".redact.json")
    v = run("verify", tmp_path / "leak.pdf", "--builtin", "IBAN")
    assert v.returncode == 1 and "[FAIL] raw-leak" in v.stdout and "<value>" in v.stdout, v.stdout
    assert "DE89" not in v.stdout and "3704" not in v.stdout  # never echo the value
    # without the original the value check cannot run -> fail closed
    v = run("verify", tmp_path / "leak.pdf", "--builtin", "IBAN", "--original", tmp_path / "gone.pdf")
    assert v.returncode == 1 and "original file not found" in v.stdout


def test_image_heavy_page_flagged(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200), False)
    pix.set_rect(pix.irect, (230, 230, 230))
    page.insert_image(pymupdf.Rect(40, 150, 560, 700), pixmap=pix)  # ~57% of the page
    page.insert_text((60, 72), "Caption: Max Weber, scan of the contract below with plenty of digital text", fontsize=11)
    doc.save(tmp_path / "h.pdf")
    r = run("extract", tmp_path / "h.pdf", "--json", check=True)
    res = json.loads(r.stdout)
    assert res["image_heavy_pages"] == [1] and res["needs_ocr_pages"] == []
    txt = run("extract", tmp_path / "h.pdf", check=True).stdout
    assert "MOSTLY IMAGES" in txt and "read by OCR" in txt
    r = run("apply", tmp_path / "h.pdf", "--terms", "Max Weber", "-o", tmp_path / "ho.pdf", check=True)
    assert "searched by OCR" in r.stdout and "look over those pages" in r.stdout
    # a normal letter is not flagged
    assert json.loads(run("extract", make_letter(tmp_path / "l.pdf"), "--json").stdout)["image_heavy_pages"] == []


def make_image_name_pdf(path):
    """Text page + an embedded image that shows "Jonas Albrecht" as pixels + a QR-like barcode image."""
    src = pymupdf.open()
    sp = src.new_page(width=300, height=80)
    sp.insert_text((10, 50), "Jonas Albrecht", fontsize=28, fontname="hebo")
    name_png = sp.get_pixmap(dpi=150).tobytes("png")
    qr = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 120, 120), False)
    qr.clear_with(255)
    for y in range(0, 120, 8):
        for x in range(0, 120, 8):
            if (x * 7 + y * 13) % 3 == 0:
                qr.set_rect(pymupdf.IRect(x, y, x + 8, y + 8), (0,))
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 80), "Reisender: Jonas Albrecht", fontsize=11, fontname="helv")
    page.insert_text((60, 100), "Gueltig fuer die einfache Fahrt.", fontsize=11, fontname="helv")
    page.insert_image(pymupdf.Rect(60, 300, 360, 380), stream=name_png)
    page.insert_image(pymupdf.Rect(400, 60, 520, 180), stream=qr.tobytes("png"))
    doc.save(path)
    return path


@pytest.mark.skipif(not redact.have_tesseract(), reason="tesseract not installed")
def test_name_inside_image_is_found_and_redacted(tmp_path):
    pdf = make_image_name_pdf(tmp_path / "ticket.pdf")
    ex = run("extract", pdf, "--full", check=True).stdout
    assert "text INSIDE images" in ex and "Albrecht" in ex.split("text INSIDE images")[1]
    ex = run("extract", pdf, check=True).stdout  # compact: the image repeats a name of the text layer -> not shown
    assert "text INSIDE images" not in ex
    assert "barcode/QR code at" in ex
    out = tmp_path / "ticket_redacted.pdf"
    ap = run("apply", pdf, "--terms", "Jonas", "-o", out, check=True).stdout
    assert "Jonas: 2" in ap and "inside images" in ap and "barcode/QR code(s) kept" in ap
    v = run("verify", out, "--terms", "Jonas")
    assert v.returncode == 0 and "[PASS] image-ocr" in v.stdout, v.stdout
    # the check really reads the images: an output with the image untouched fails
    weak = tmp_path / "weak_redacted.pdf"
    run("apply", pdf, "--terms", "Reisender", "-o", weak, check=True)
    v = run("verify", weak, "--terms", "Jonas")
    assert v.returncode == 1 and "[FAIL] image-ocr" in v.stdout, v.stdout


@pytest.mark.skipif(not redact.have_tesseract(), reason="tesseract not installed")
def test_barcodes_option_blacks_out_barcode(tmp_path):
    pdf = make_image_name_pdf(tmp_path / "ticket.pdf")
    out = tmp_path / "ticket_redacted.pdf"
    ap = run("apply", pdf, "--terms", "Jonas Albrecht", "--barcodes", "-o", out, check=True).stdout
    assert "barcodes: 1" in ap and "code(s) kept" not in ap
    pix = pymupdf.open(out)[0].get_pixmap(clip=pymupdf.Rect(410, 70, 510, 170), dpi=36, colorspace=pymupdf.csGRAY)
    assert max(pix.samples) < 60  # the whole barcode area is black


def test_extract_compacts_repeated_pages_and_limits_output(tmp_path):
    doc = pymupdf.open()
    boiler = [f"Bedingung {k}: Diese Fahrkarte gilt nur mit amtlichem Lichtbildausweis und BahnCard." for k in range(30)]
    for n in range(12):
        page = doc.new_page()
        page.insert_text((40, 40), f"Reisender: Person{n} Nachname{n}", fontsize=9)
        for k, ln in enumerate(boiler):
            page.insert_text((40, 60 + k * 12), ln, fontsize=8)
    pdf = tmp_path / "many.pdf"
    doc.save(pdf)
    full = run("extract", pdf, "--full", check=True).stdout
    compact = run("extract", pdf, check=True).stdout
    assert len(compact) < len(full) / 4
    for n in range(12):
        assert f"Person{n} Nachname{n}" in compact  # unique lines of every page survive
    assert "repeated from earlier pages omitted" in compact
    one = run("extract", pdf, "--pages", "5", check=True).stdout
    assert "Person4 Nachname4" in one and "Person3" not in one and "Bedingung 7" in one


def test_extract_output_limit_points_to_pages(tmp_path):
    doc = pymupdf.open()
    for n in range(8):
        page = doc.new_page()
        for k in range(55):
            page.insert_text((30, 30 + k * 14), f"Seite {n} Zeile {k} eindeutiger Inhalt Nummer {n * 100 + k}", fontsize=8)
    pdf = tmp_path / "long.pdf"
    doc.save(pdf)
    out = run("extract", pdf, check=True).stdout
    assert "OUTPUT LIMIT" in out and "--pages" in out
    assert len(out) < redact.EXTRACT_MAX_CHARS + 6000


def test_label_redacts_values_after_label_and_apply_verifies(tmp_path):
    doc = pymupdf.open()
    for n in range(3):
        page = doc.new_page()
        page.insert_text((50, 60), f"Auftragsnummer: 77{n}1234   Datum: 0{n + 1}.03.2026", fontsize=11)
        page.insert_text((50, 80), f"Auftragsnr. 99{n}876", fontsize=11)
        page.insert_text((50, 100), "Reisender: Lena Hofmann", fontsize=11)
    pdf = tmp_path / "orders.pdf"
    doc.save(pdf)
    out = tmp_path / "orders_redacted.pdf"
    r = run("apply", pdf, "--label", "Auftragsnummer", "--label", "Auftragsnr", "--mode", "text", "-o", out)
    assert r.returncode == 0 and "VERIFY PASSED" in r.stdout, r.stdout
    assert "label:Auftragsnummer: 3" in r.stdout and "label:Auftragsnr: 3" in r.stdout
    assert (tmp_path / "orders_redacted.pdf.verified").exists()  # apply writes the marker itself
    t = page_text(out)
    for n in range(3):
        assert f"77{n}1234" not in t and f"99{n}876" not in t
        assert f"0{n + 1}.03.2026" in t  # the next field on the line stays
    assert "Auftragsnummer" in t and "Lena Hofmann" in t
    assert run("verify", out, "--label", "Auftragsnummer", "--mode", "text").returncode == 0


def make_security_background_pdf(path):
    """Like a DB ticket: a light-grey, slightly rotated text pattern with name + order number as an IMAGE, a big
    grey copy of the order number as an image, and the labelled order number + name in the text layer."""
    bg = pymupdf.open()
    bp = bg.new_page(width=360, height=160)
    for k in range(9):
        bp.insert_text((-20, 20 + k * 18), "Fahrkarte Hamburg Hbf Lena Hofmann 318204657731 Super Sparpreis " * 2,
                       fontsize=13, color=(0.82, 0.82, 0.82), morph=(pymupdf.Point(180, 80), pymupdf.Matrix(-10)))
    bp.insert_text((120, 60), "Lena Hofmann", fontsize=14, fontname="hebo")
    bg_png = bp.get_pixmap(dpi=150).tobytes("png")
    num = pymupdf.open()
    np_ = num.new_page(width=260, height=60)
    np_.insert_text((10, 45), "318204657731", fontsize=40, color=(0.75, 0.75, 0.75), fontname="hebo")
    num_png = np_.get_pixmap(dpi=150).tobytes("png")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 80), "Reisende: Lena Hofmann", fontsize=11)
    page.insert_text((60, 100), "Auftragsnummer: 318204657731", fontsize=11)
    page.insert_text((60, 120), "Bitte Lichtbildausweis mitfuehren.", fontsize=11)
    page.insert_image(pymupdf.Rect(330, 60, 530, 106), stream=num_png)
    page.insert_image(pymupdf.Rect(100, 600, 460, 760), stream=bg_png)
    doc.save(path)
    return path


@pytest.mark.skipif(not redact.have_tesseract(), reason="tesseract not installed")
def test_faint_rotated_background_and_grey_number_are_removed(tmp_path):
    pdf = make_security_background_pdf(tmp_path / "ticket.pdf")
    out = tmp_path / "ticket_redacted.pdf"
    ap = run("apply", pdf, "--terms", "Lena Hofmann", "--label", "Auftragsnummer", "-o", out)
    assert ap.returncode == 0 and "VERIFY PASSED" in ap.stdout, ap.stdout
    assert "removed completely" in ap.stdout, ap.stdout
    full = run("verify", out, "--terms", "Lena Hofmann", "--label", "Auftragsnummer", "--original", pdf)
    assert full.returncode == 0 and "incl. faint, rotated and mirrored" in full.stdout, full.stdout
    page = pymupdf.open(out)[0]
    for clip in (pymupdf.Rect(334, 64, 526, 102), pymupdf.Rect(114, 614, 446, 746)):
        px = page.get_pixmap(clip=clip, dpi=72, colorspace=pymupdf.csGRAY).samples
        grey = sum(1 for b in px if 40 < b < 235)
        assert grey < 0.02 * len(px), grey  # only white + the black bar left: no grey pattern/number
    assert "Lichtbildausweis" in run("extract", pdf, check=True).stdout  # unrelated text untouched (source)


def test_letter_spaced_name_is_found(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    x = 60
    for ch in "Jürgen Müller":
        page.insert_text((x, 100), ch, fontsize=11)
        x += 9
    page.insert_text((60, 140), "Andere Zeile bleibt.", fontsize=11)
    pdf = tmp_path / "spaced.pdf"
    doc.save(pdf)
    out = tmp_path / "spaced_redacted.pdf"
    ap = run("apply", pdf, "--terms", "Jürgen Müller", "--mode", "text", "-o", out, check=True).stdout
    assert "Jürgen Müller: 1" in ap
    t = page_text(out)
    assert "J" not in t.split("Andere")[0] and "Andere Zeile bleibt." in t


def test_retention_sweep_only_old_pdfs_and_sidecars(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    old = time.time() - 3 * 3600
    for name in ("old.pdf", "old_redacted.pdf.redact.json", "old_redacted.pdf.verified", "notes.txt"):
        (home / name).write_text("x")
        os.utime(home / name, (old, old))
    (home / "new.pdf").write_text("x")
    env = {**os.environ, "HOME": str(home), "REDACT_RETENTION_MIN": "60"}
    subprocess.run([*REDACT, "builtins"], capture_output=True, text=True, env=env, check=True)
    assert sorted(p.name for p in home.iterdir()) == ["new.pdf", "notes.txt"]
    env["REDACT_RETENTION_MIN"] = "0"  # off
    (home / "old2.pdf").write_text("x")
    os.utime(home / "old2.pdf", (old, old))
    subprocess.run([*REDACT, "builtins"], capture_output=True, text=True, env=env, check=True)
    assert (home / "old2.pdf").exists()


def test_label_takes_address_and_names_and_medical_builtins(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    for k, t in enumerate(["Patient: Erika Musterfrau, geb. 14.02.1961", "Anschrift: Birkenweg 3, 40489 Düsseldorf",
                           "Versichertennummer: A123456789", "Diagnose: Akuter NSTEMI (I21.4)",
                           "Aufnahme am 02.03.2026 durch Dr. med. Hannes Kröger", "Klinik: Parkstraße 4, 40210 Düsseldorf"]):
        page.insert_text((50, 80 + k * 18), t, fontsize=11)
    page.insert_text((50, 800), "Musterfrau, Erika * 14.02.1961", fontsize=8)
    pdf = tmp_path / "med.pdf"
    doc.save(pdf)
    out = tmp_path / "med_redacted.pdf"
    r = run("apply", pdf, "--mode", "text", "--label", "Patient", "Anschrift", "--builtin", "BIRTHDATE",
            "HEALTH_INSURANCE_DE", "ICD10", "-o", out)
    assert r.returncode == 0 and "VERIFY PASSED" in r.stdout, r.stdout
    t = page_text(out)
    for gone in ("Erika Musterfrau", "Musterfrau, Erika", "14.02.1961", "Birkenweg", "40489", "A123456789", "I21.4"):
        assert gone not in t, gone
    for kept in ("Akuter NSTEMI", "02.03.2026", "Hannes Kröger", "Parkstraße 4, 40210 Düsseldorf", "Anschrift:"):
        assert kept in t, kept


def test_address_option_takes_the_block_below_the_name(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    for k, t in enumerate(["Nordlicht Haustechnik GmbH, Hafenstraße 9, 20457 Hamburg", "", "Herrn", "Jonas Albrecht",
                           "Lindenallee 17", "50667 Köln", "Köln, 12.03.2026", "Sehr geehrter Herr Albrecht,",
                           "die Wartung in der Ringstraße 4 ist erledigt."]):
        page.insert_text((50, 80 + k * 16), t, fontsize=11)
    pdf = tmp_path / "inv.pdf"
    doc.save(pdf)
    out = tmp_path / "inv_redacted.pdf"
    r = run("apply", pdf, "--mode", "text", "--terms", "Jonas Albrecht", "--address", "-o", out)
    assert r.returncode == 0 and "VERIFY PASSED" in r.stdout and "address: 2" in r.stdout, r.stdout
    t = page_text(out)
    for gone in ("Jonas Albrecht", "Lindenallee", "50667", "Herr Albrecht"):
        assert gone not in t, gone
    for kept in ("Hafenstraße 9, 20457 Hamburg", "Köln, 12.03.2026", "Ringstraße 4", "Herrn"):
        assert kept in t, kept


def test_more_adds_to_previous_targets(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    run("apply", pdf, "--terms", "Max Weber", "--mode", "text", check=True)
    assert (tmp_path / "l.pdf.targets.json").exists()
    r = run("apply", pdf, "--more", "--builtin", "IBAN", check=True)
    assert "Max Weber: 1" in r.stdout and "builtin:IBAN: 1" in r.stdout and "VERIFY PASSED" in r.stdout, r.stdout
    t = page_text(tmp_path / "l_redacted.pdf")
    assert "Max Weber" not in t and "DE89" not in t and "Erika Mustermann" in t  # mode text kept from stage 1
    assert run("apply", make_letter(tmp_path / "n.pdf"), "--more", "--terms", "X").returncode == 2  # no earlier stage


def test_address_finds_labelled_field_in_data_block_and_takes_values(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    for k, t in enumerate(["Patient: Erika Musterfrau, geb. 14.02.1961", "Versichertennummer: A123456789",
                           "Fallnummer: 2026-778812", "Anschrift: Birkenweg 3, 40489 Düsseldorf",
                           "Diagnosen: Hypertonie", "Klinik: Parkstraße 4, 40210 Düsseldorf"]):
        page.insert_text((50, 80 + k * 16), t, fontsize=11)
    pdf = tmp_path / "m.pdf"
    doc.save(pdf)
    out = tmp_path / "m_redacted.pdf"
    r = run("apply", pdf, "--mode", "text", "--terms", "Erika Musterfrau", "--address", "-o", out)
    assert r.returncode == 0 and "address: 1" in r.stdout, r.stdout
    t = page_text(out)
    assert "Birkenweg" not in t and "40489" not in t and "Anschrift:" in t and "Parkstraße 4" in t
    out2 = tmp_path / "m2_redacted.pdf"
    r = run("apply", pdf, "--mode", "text", "--address", "Birkenweg 3, 40489 Düsseldorf", "-o", out2)
    assert r.returncode == 0 and "Birkenweg" not in page_text(out2), r.stdout + r.stderr


def test_not_found_lists_real_values_of_the_same_kind(tmp_path):
    pdf = make_letter(tmp_path / "l.pdf")
    r = run("apply", pdf, "--terms", "Max Weber", "DE12 3456 7890 1234 5678 90", "info@erfunden.de", "Kölner Str. 42")
    assert r.returncode == 0 and "NOT FOUND" in r.stdout, r.stdout
    assert "looks like IBAN" in r.stdout and "DE89 3704 0044 0532 0130 00" in r.stdout and "--builtin IBAN" in r.stdout
    assert "looks like EMAIL" in r.stdout and "erika.mustermann@example.de" in r.stdout
    assert "never guess" in r.stdout
