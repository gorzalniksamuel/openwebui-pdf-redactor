# Testing

Three levels:

1. **Unit / integration tests** (`open-terminal/tests/test_redact.py`, pytest, ~40 tests, ~1 min): every PDF type and
   feature, generated in-test (names inside images, faint rotated security backgrounds, grey number copies,
   barcodes, letter-spaced text, labels, scans, second stage on a redacted output, retention, verify must fail on
   leftovers).
   ```bash
   cd open-terminal && REDACT_FIXTURES=../testing/fixtures python3 -m pytest -q tests
   ```
2. **Leak campaign** (`testing/run_matrix.py`): each case is redacted with `redact apply` and the output is examined by
   an **independent** checker that does not use the redactor's libraries (`check.py`: poppler `pdftotext`/`pdfimages`/
   `pdftoppm`, `qpdf`, Tesseract, Pillow). It reports a leak if any secret is found in
   - the text layer (two extractors), raw file bytes and the fully decompressed file (Latin-1, UTF-8, UTF-16BE, hex
     strings), document info, XMP
   - any embedded image (OCR) and the rendered pages (OCR at 300 dpi, plus rotated ±10°, high-contrast and mirrored
     variants; any 6-digit piece of a long number counts)
   - structure: outlines, forms, attachments, JavaScript, annotations, optional content, more than one revision
   and it checks that the words that must stay are still readable (no over-redaction).
   ```bash
   docker build -t redact-checker -f testing/Dockerfile.checker testing
   python3 testing/run_matrix.py                    # redact + generator with the local python (pymupdf etc.)
   # or everything in containers (hardened terminal image from open-terminal/docker/):
   W=$PWD/testing/work; mkdir -p $W && chmod 777 $W
   python3 testing/run_matrix.py \
     --python "docker run --rm -u 1000:1000 -v $W:$W --entrypoint python3 pdf-redactor-terminal" \
     --redact "docker run --rm -u 1000:1000 -v $W:/w -w /w --entrypoint redact pdf-redactor-terminal"
   ```
   Cases (`testing/cases.py`): letters, invoices, forms, incremental updates with an old revision, mixed scan/digital,
   rotated pages, owner-encrypted, scans with OCR layer, damaged xref, plain and rotated scans, hidden places
   (invisible/white/1 pt text, text under an image, hidden layer, metadata, XMP, bookmark, annotation, attachment, form
   field, JavaScript, link), multi-page ticket sets (names in text + images, barcodes, labels), tricky text (umlauts,
   hyphenated and upper-case names, line breaks inside names, letter spacing, vertical text, Unicode/CID fonts,
   tables), rotated page with image, second stage on an already redacted output, 50 pages.
3. **Chat tests** (manual or Playwright): real conversations in Open WebUI, e.g.
   "redact all first names" → answer the barcode question → "remove all order numbers as well"; check that every
   reply has a download card, that each stage contains all redactions so far, and run the checker on the downloads.

## Results (reference run 2026-10-26, Open WebUI 0.11.4, Open Terminal, qwen3.5:9b, 4 CPU cores)

**Unit tests:** 42/42 passed.

**Leak campaign:** 30/30 cases clean (24 in `testing/cases.py` plus 6 on private real documents: a DB ticket and a
7-page ticket set, first names / names / order numbers / ticket codes, second stage on a redacted output). Times per
document: text PDFs about 1 s, invoices/letters with images 3-7 s, scans 5-9 s, 7-page ticket set with security
backgrounds about 45 s, 50 pages about 20 s.

**Chat tests** (6 scenarios, 11 turns, run twice = 22 turns per round; several rounds while tuning):

| Scenario | Turns | Result of the final rounds |
|---|---|---|
| Ticket: first names → + order number → + last name | 3 | all stages verified and clean; in 1 of 2 runs the 9b model asked for the last name in stage 3 instead of reading the document again (fixed by keeping the extract start in the history) |
| Invoice: customer data → + issuer bank account → + e-mail | 3 | clean; the model sometimes takes "customer data" too broadly (also issuer data) or tries invented values - `apply` reports NOT FOUND with the real values, nothing wrong is claimed as redacted |
| Redacted output re-uploaded: + last name | 1 | clean (OCR of the flattened page) |
| Medical letter: patient data → + ICD codes | 2 | clean (name also as "Last, First", birth date, insurance no., case no., address) |
| Lab scan: anonymise the patient | 1 | clean |
| English invoice: names, e-mails, phones, credit card | 1 | clean |

Download card after a redaction: 20-22 of 22 turns per round (a missing card = the model ran no new apply: either the
item was already redacted, or it asked a question). Context per request: 10.7k-13.8k tokens (limit 16k).

**Takeaway:** the redaction itself (what is found, what is removed, what is verified) was leak-free in every case once
the targets were right. Remaining errors come from the 9b model choosing targets: occasionally too broad, or a missing
item that the user then names in a follow-up. A larger tool-capable model reduces this.

Known checker noise: OCR may misread a keep word (e.g. `ß`, `ä`, a word next to a black bar) and report it missing
although it is visible; and 6-digit pieces of a phone number can collide with other numbers. Look at the page before
treating it as over-redaction or a leak.
