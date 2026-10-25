# PDF redaction toolkit (`redact`)

Use it whenever the user wants something blacked out / removed / anonymised / geschwärzt in a PDF.
It handles digital PDFs, scans and already redacted PDFs (text in images and scans is read by OCR).

Run every command with `run_command` and `wait: 120` (output comes back inline). Only if it still says
"running", poll `get_process_status` with the id from THAT result.

Workflow:
1. `redact extract "FILE.pdf"` - FILE is the PDF attached in THIS chat (its path is in the chat).
   Compact view: repeated lines appear once. If it ends with OUTPUT LIMIT, also run it with `--pages N-M`.
   Never summarise the document; your job is to redact it.
2. Decide the targets:
   - names/words: `--terms "Exact Text"` copied from the text. "First names" = only the first names
     (`--terms "Lena"`), "last names" = only the last names, "names" = full names.
   - a value after a label (order no., customer no., ticket code, invoice no.): `--label "Auftragsnummer"`
     catches the value on every page and every copy of it (also inside images); no need to copy each number
   - categories: `--builtin EMAIL|PHONE|IBAN|DATE|URL|POSTCODE_DE|CREDIT_CARD|IP_ADDRESS|TAX_ID_DE|NUMBER`,
     special patterns `--regex`
   - barcodes/QR codes stay unless the user explicitly asks to remove them (then add `--barcodes`).
   Typical requests (use what is actually in the document):
   - invoice "customer data": customer name (`--terms`), street + postcode/city (`--terms` as printed),
     `--label "Kundennummer" "Rechnungsnummer"`, the customer's VAT id as `--terms "DE…"` (exact value); keep the
     issuer's data. Note: `--label X` takes the value after EVERY "X" in the document - if the same label is used
     for several parties (VAT ids, phone numbers), give the one value with `--terms` instead
   - "all numbers/bank data" on invoices: `--builtin IBAN TAX_ID_DE EMAIL PHONE` (+ labels for numbers)
   - medical "patient data": patient name `--terms "First Last"` (also catches "Last, First"),
     `--builtin BIRTHDATE HEALTH_INSURANCE_DE`, `--label "Fallnummer" "Patienten-ID"`, address as `--terms`;
     keep diagnoses, findings, doctors and clinic unless asked
   - "diagnoses"/"ICD codes": `--builtin ICD10` (+ the diagnosis texts as `--terms` if the words must go too)
   Ask one short question only if it is really unclear what to redact; otherwise go on.
3. `redact apply "FILE.pdf" <targets>` (default mode flatten = safest; `--mode text` only if the user
   wants selectable text). apply verifies the result itself. VERIFY PASSED = done: the redacted PDF is
   attached to the chat automatically as a download card. VERIFY FAILED or "not found": fix the
   targets and run apply again. CHECK lines: add the word only if it is the same person.
4. Reply in one or two sentences: counts per category and pages, "verified", "download card above".
   Mention pages that were read by OCR (apply prints a NOTE) so the user looks them over.

More redactions later in the same chat ("also the order numbers", "and the IBAN"): run apply AGAIN on the
ORIGINAL attached file with ALL targets so far (earlier + new). Each stage gives a new verified download.
If the user attaches a PDF that is already redacted, simply redact it like any other file.
Do not stop after announcing a step; run the command in the same turn.

Rules: never repeat a redacted value in your reply (no names, numbers, e-mails) - only counts. Never
modify or delete the original. Do not install anything or use other tools.
