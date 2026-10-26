"""Independent leak checker (host side: poppler, qpdf, tesseract; NOT PyMuPDF like the redactor).
usage: check.py OUT.pdf --secret S [--secret S ...] [--keep K ...] [--pages N] -> JSON on stdout"""
import argparse, glob, json, os, re, subprocess, tempfile, unicodedata


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, timeout=600, **kw)


def norm(s):
    s = unicodedata.normalize("NFKC", s).casefold()
    return re.sub(r"\s+", " ", s).strip()


def variants(secret):
    n = norm(secret)
    return {n, n.replace(" ", "")}


def fold(s):
    """OCR-tolerant form: no diacritics, ß -> ss (tesseract often reads ä as a, ß as B/f)."""
    s = unicodedata.normalize("NFKD", s.replace("ß", "ss").replace("ẞ", "SS"))
    return "".join(c for c in s if not unicodedata.combining(c))


def found(secret, text):
    t = norm(text)
    return any(v and (v in t or v in t.replace(" ", "")) for v in variants(secret))


def readable(keep, text):
    """Keep words: also accept OCR misreads of umlauts/ß (b, B, f, 8 for ß)."""
    if found(keep, text):
        return True
    k, t = fold(norm(keep)), fold(norm(text))
    if k in t:
        return True
    pat = re.escape(k).replace("ss", "(?:ss|s|b|f|8)")
    return re.search(pat, t) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--secret", action="append", default=[])
    ap.add_argument("--keep", action="append", default=[])
    ap.add_argument("--pages", type=int)
    a = ap.parse_args()
    res = {"file": os.path.basename(a.pdf), "leaks": [], "structure": [], "keep_missing": [], "info": {}}
    work = tempfile.mkdtemp(prefix="chk-")
    raw = open(a.pdf, "rb").read()

    # 1. document info / XMP / page count
    info = sh(["pdfinfo", a.pdf]).stdout.decode("latin-1")
    pages = int(re.search(r"^Pages:\s+(\d+)", info, re.M).group(1)) if "Pages:" in info else 0
    res["info"]["pages"] = pages
    if a.pages is not None and pages != a.pages:
        res["structure"].append(f"page count {pages} != {a.pages}")
    for key in ("Title", "Subject", "Keywords", "Author", "Creator", "Producer"):
        m = re.search(rf"^{key}:\s+(.*)$", info, re.M)
        if m and m.group(1).strip():
            res["structure"].append(f"Info {key}={m.group(1).strip()[:40]!r}")
    if "Encrypted:     yes" in info:
        res["structure"].append("encrypted")
    if sh(["pdfinfo", "-meta", a.pdf]).stdout.strip():
        res["structure"].append("XMP metadata present")

    # 2. text layer, two extractors (poppler default + raw order)
    for mode in ([], ["-raw"]):
        txt = sh(["pdftotext", *mode, a.pdf, "-"]).stdout.decode("utf-8", "replace")
        res["info"]["text_chars" + ("_raw" if mode else "")] = len(txt.strip())
        for s in a.secret:
            if found(s, txt):
                res["leaks"].append(f"text{'(raw)' if mode else ''}: {s}")

    # 3. raw bytes: original file + qpdf fully decompressed; latin-1, utf-8, utf-16be, hex strings
    qdf = os.path.join(work, "q.pdf")
    sh(["qpdf", "--qdf", "--object-streams=disable", "--decode-level=all", a.pdf, qdf])
    blobs = {"file": raw, "decompressed": open(qdf, "rb").read() if os.path.exists(qdf) else b""}
    for where, b in blobs.items():
        hexes = b"".join(bytes.fromhex(h.decode()) for h in re.findall(rb"<([0-9A-Fa-f\s]{4,})>", b)
                         if len(re.sub(rb"\s", b"", h)) % 2 == 0)
        for s in a.secret:
            for enc in ("latin-1", "utf-8", "utf-16-be"):
                try:
                    needle = s.encode(enc)
                except UnicodeEncodeError:
                    continue
                if needle in b or needle in hexes or needle.lower() in b.lower():
                    res["leaks"].append(f"raw-{where}({enc}): {s}")
                    break
    d = blobs["decompressed"]
    for key in (b"/EmbeddedFiles", b"/JavaScript", b"/JS ", b"/AcroForm", b"/Annots", b"/OCProperties",
                b"/OpenAction", b"/Outlines", b"/Metadata", b"/Names"):
        if key in d:
            res["structure"].append(f"{key.decode().strip()} present")
    if raw.count(b"%%EOF") != 1 or re.search(rb"/Prev\s", raw):
        res["structure"].append(f"multiple revisions (%%EOF x{raw.count(b'%%EOF')})")
    chk = sh(["qpdf", "--check", a.pdf])
    if chk.returncode not in (0, 3):
        res["structure"].append("qpdf --check failed")

    # 4. every embedded image, OCR
    sh(["pdfimages", "-png", a.pdf, os.path.join(work, "img")])
    img_txt = ""
    for f in sorted(glob.glob(os.path.join(work, "img-*.png"))):
        img_txt += sh(["tesseract", f, "-", "-l", "deu+eng", "--psm", "11"]).stdout.decode("utf-8", "replace") + "\n"
    for s in a.secret:
        if found(s, img_txt):
            res["leaks"].append(f"ocr-embedded-image: {s}")

    # 5. what a reader sees: full-page render (300 dpi), OCR; keep words must still be readable
    sh(["pdftoppm", "-r", "300", "-png", a.pdf, os.path.join(work, "pg")])
    page_txt = ""
    for f in sorted(glob.glob(os.path.join(work, "pg-*.png"))):
        page_txt += sh(["tesseract", f, "-", "-l", "deu+eng"]).stdout.decode("utf-8", "replace") + "\n"
    for s in a.secret:
        if found(s, page_txt):
            res["leaks"].append(f"ocr-rendered-page: {s}")
    # 6. faint / rotated / mirrored text: page renders rotated +-10 deg, contrast threshold, mirrored (Pillow)
    from PIL import Image, ImageOps
    var_txt = ""
    for f in sorted(glob.glob(os.path.join(work, "pg-*.png"))):
        g = ImageOps.grayscale(Image.open(f))
        for k, im in enumerate([g.point(lambda v: 0 if v < 235 else 255), g.rotate(10, fillcolor=255, expand=True),
                                g.rotate(10, fillcolor=255, expand=True).point(lambda v: 0 if v < 235 else 255),
                                g.rotate(-10, fillcolor=255, expand=True), ImageOps.mirror(g)]):
            vf = os.path.join(work, f"v{k}.png")
            im.save(vf)
            var_txt += sh(["tesseract", vf, "-", "-l", "deu+eng", "--psm", "11"]).stdout.decode("utf-8", "replace") + "\n"
    for s in a.secret:
        if found(s, var_txt) and not found(s, page_txt):
            res["leaks"].append(f"ocr-variant(faint/rotated/mirrored): {s}")
    # pieces of long plain numbers (order numbers, account numbers); not of phone numbers etc. with separators
    digits = [s for s in a.secret if re.fullmatch(r"\d{10,}", s.strip())]
    flat = re.sub(r"\D", "", var_txt + page_txt + img_txt)
    for d in digits:
        pieces = [d[i:i + 6] for i in range(len(d) - 5)]
        if any(p in flat for p in pieces):
            res["leaks"].append(f"ocr-number-fragment(6+ digits): {d[:3]}...")
    for k in a.keep:
        if not readable(k, page_txt):
            res["keep_missing"].append(k)
    subprocess.run(["rm", "-rf", work])
    res["ok"] = not res["leaks"] and not res["structure"]
    print(json.dumps(res, ensure_ascii=False))


main()
