# How the redactor works

## `redact` CLI

```
redact extract FILE.pdf [--pages 2-5] [--full] [--json]
redact apply   FILE.pdf [targets] [--mode flatten|text] [--barcodes] [-o OUT.pdf] [--json]
redact verify  OUT.pdf  [targets] [--original FILE.pdf] [--json]
targets: --terms "Exact Text" ... | --label "Auftragsnummer" ... | --builtin EMAIL PHONE IBAN ... | --regex "..."
```

| Target | Matches |
|---|---|
| `--terms "Erika Musterfrau"` | the text, case-insensitive, across line breaks and letter spacing; 2-3 word names also inverted ("Musterfrau, Erika"); a surname after Herr/Frau/Dr./Mr. is added |
| `--label "Auftragsnummer"` | the value after every such label on the same line: a code/number with its digit groups, or a name of up to 3 capitalised words (titles skipped). Every value found is also searched literally everywhere else (text and images) |
| `--builtin` | `EMAIL PHONE IBAN DATE BIRTHDATE URL POSTCODE_DE CREDIT_CARD IP_ADDRESS TAX_ID_DE HEALTH_INSURANCE_DE ICD10 NUMBER` (`BIRTHDATE` = a date after geb./Geburtsdatum/born/DOB/*; `TAX_ID_DE` = tax id, Steuernummer, DE VAT id; `HEALTH_INSURANCE_DE` = Krankenversichertennummer) |
| `--regex` | a Python regular expression |
| `--barcodes` | every barcode/QR code (only on explicit request) |

### extract
Page text (compact: lines repeated from earlier pages shown once, output capped at ~9,000 characters with a
`--pages` hint), OCR text of images and of pages without a text layer (scans, already redacted PDFs; page
orientation detected), barcode/QR positions.

### apply
1. **Find** every occurrence:
   - text layer: characters with their boxes; pieces on the same baseline are merged (letter-spaced text), terms
     match case-insensitively across line breaks and letter spacing
   - `--label X`: the value after the label on the same line, on every page; every matched value of a label, regex or
     builtin is then also searched literally everywhere else (the same number printed again without its label, or
     inside an image)
   - images and scans: Tesseract word boxes (rendered in the displayed orientation; page images with orientation
     detection), matched with the same patterns
   - a surname standing alone after Herr/Frau/Dr./Mr. is added when the full name is a target
2. **Redact** (MuPDF true redaction, two passes): image pixels and line art under the boxes, then the glyphs.
3. **Images** (anything smaller than half a page): an image is removed completely (pixels only; text drawn over it
   stays; the spot is outlined) when
   - the search found a target inside it (it holds the data, maybe also as faint copies no OCR reads), or
   - it repeats this page's data (>= 3 words/numbers of the text layer, e.g. a ticket's security background with
     route, date, order number) while the page has targets, or
   - a leftover scan reveals a piece of a target (a term, a word of a multi-word name, a matched value, any 5-digit
     piece of a long number) in one of several OCR variants (rotated ±10°, contrast threshold for light grey,
     mirrored).
   OCR words lying over text-layer text are ignored for these decisions. Barcodes are only touched with `--barcodes`.
   Page-sized images (scans) are never wiped: they get the same patterns again on high-contrast OCR variants.
4. **Flatten** (default): Ghostscript renders every page at 200 dpi into a new image-only PDF (hidden layers dropped as
   in a viewer). `--mode text` keeps the remaining text selectable.
5. **Rewrite + scrub**: Ghostscript pdfwrite, then metadata, XMP, JavaScript, attachments, annotations, links,
   outlines, forms removed; non-incremental save (one revision).
6. **Verify** (built in): the checks below; only then `<out>.verified` (SHA-256) is written.

### verify (hard checks)
| Check | Meaning |
|---|---|
| open | opens, page count unchanged, `qpdf --check` passes |
| text-leak | flatten: no extractable text at all; text mode: no target/pattern in the text |
| residual | no remaining glyph centre inside a redaction box |
| image-ocr | no target readable by OCR in any former image area: pieces (single name words, 5-digit number pieces) in smaller images, whole names/values in page images (standalone verify: all variants) |
| raw-leak | no target or matched value in decompressed streams, strings or file bytes (Latin-1, UTF-8, UTF-16BE) |
| metadata | no Info entries, no XMP |
| structure | no outlines, forms, embedded files, JavaScript, OpenAction, annotations |
| single-revision | one `%%EOF`, no `/Prev` |

### Performance (4 CPU cores)
Text PDFs: about 1 s per document. Pages with images: 1-7 s per page (OCR). Scans: about 3-6 s per page.
OCR runs as one parallel batch over all pages (Tesseract, one process per core).

## Open WebUI side
- **Terminal connection** with chat uploads = filesystem: the PDF goes into the terminal untouched.
- **AGENTS.md** (from the terminal home) is added to every turn: the workflow for the model.
- **Filter** `pdf_redactor_filter`: enables the delivery tool, trims old tool output (16k context), attaches verified
  outputs automatically after each reply.
- **Tool** `pdf_redactor_delivery`: explicit delivery on request (same checks).

## Limits
- What counts as "a first name" is decided by the LLM. Check the result; ask for a correction in the same chat.
- OCR can miss unusual fonts or handwriting on scans. The output is verified by OCR as well, but OCR cannot prove the
  absence of text it cannot read: look over pages flagged as OCR pages.
- Barcodes/QR codes are kept unless the user asks to remove them (`--barcodes`); they can encode personal data
  (e.g. train tickets).
- When an image contains traces of a target, the whole image is removed (more than strictly necessary, but the only
  reliable way for faint/rotated background patterns).
