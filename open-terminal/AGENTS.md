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
   - categories: `--builtin EMAIL|PHONE|IBAN|DATE|BIRTHDATE|URL|POSTCODE_DE|CREDIT_CARD|IP_ADDRESS|TAX_ID_DE|
     HEALTH_INSURANCE_DE|ICD10|NUMBER`,
     special patterns `--regex`
   - barcodes/QR codes stay unless the user explicitly asks to remove them (then add `--barcodes`).
   Checklist for "personal data" / "Kundendaten" / "Patientendaten" / "anonymise the person" - take EVERY item
   that is in the document, for that person only:
   - name, every spelling: `--terms "Erika Musterfrau"` (also catches "Musterfrau, Erika"; "Herr/Frau X" is added)
   - address: add `--address` (takes the address block printed right below the person's name: street, postcode,
     city); if the address is elsewhere: `--label "Anschrift"` / `--label "Adresse"` when labelled, otherwise
     `--terms "Birkenweg 3" "40489 Düsseldorf"` exactly as printed (street line AND postcode line)
   - date of birth `--builtin BIRTHDATE`, insurance number `--builtin HEALTH_INSURANCE_DE`
   - IDs: `--label "Kundennummer" "Fallnummer" "Patienten-ID" "Versichertennummer"` (whatever labels exist)
   - the person's own phone/e-mail/IBAN/VAT id: `--terms "<exact value>"` (labels like "Tel." or "USt-IdNr"
     also appear for the issuer/clinic - `--label X` would take every one of them)
   Keep the issuer's/clinic's/doctors' data, diagnoses and findings unless asked.
   Other typical requests: "all numbers/bank data" on invoices: `--builtin IBAN TAX_ID_DE EMAIL PHONE`;
   "diagnoses"/"ICD codes": `--builtin ICD10` (+ diagnosis texts as `--terms` if the words must go too).
   "Anonymise the patient/customer/person" is clear: apply the whole checklist, do not ask.
   A category without an owner ("the e-mail address", "phone numbers", "IBAN") means every value of it in the
   document: use the --builtin.
   Ask one short question only if it is really unclear what to redact; otherwise go on.
3. `redact apply "FILE.pdf" <targets>` (default mode flatten = safest; `--mode text` only if the user
   wants selectable text). apply verifies the result itself. VERIFY PASSED = done: the redacted PDF is
   attached to the chat automatically as a download card. VERIFY FAILED or "not found": fix the
   targets and run apply again. CHECK lines: add the word only if it is the same person.
4. Reply in one or two sentences: counts per category and pages, "verified", "download card above".
   Mention pages that were read by OCR (apply prints a NOTE) so the user looks them over.

More redactions later in the same chat ("also the order numbers", "and the last name"): ALWAYS run
`redact apply "FILE.pdf" --more <only the new targets>` on the ORIGINAL attached file, even if you think it is
already covered. `--more` keeps every target of the previous stage. Each stage gives a new verified download.
Need the document text again (earlier tool output is shortened)? Run `redact extract` again - never ask the user
for something that is in the document.
If the user attaches a PDF that is already redacted, simply redact it like any other file.
Do not stop after announcing a step; run the command in the same turn.

Rules: never repeat a redacted value in your reply (no names, numbers, e-mails) - only counts. Never
modify or delete the original. Do not install anything or use other tools.
