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

## Results (reference run, Open WebUI 0.11.4, qwen3.5:9b)

See the table in the release notes / commit message of the version you use. The campaign must report every case clean
(no leaks, no structure findings, no missing keep words) before a change is released.

Known checker noise: OCR may misread a keep word (e.g. `ß`) and report it missing although it is visible; look at
the page before treating it as over-redaction.
