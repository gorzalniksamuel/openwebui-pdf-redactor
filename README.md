# PDF Redactor for Open WebUI + Open Terminal

Chat-based, irreversible PDF redaction. Attach a PDF in Open WebUI, say what to black out ("alle Vornamen",
"the IBAN and all e-mails", "also the order numbers"), get a verified PDF back as a download card. The redacted file
contains no recoverable trace of the removed content: no text layer, no metadata, no old revisions, no hidden objects,
and no readable copies in images.

Works with digital PDFs, scans, and PDFs that were already redacted (further stages in the same chat). The heavy
lifting is a deterministic CLI (`redact`, Python + PyMuPDF + Ghostscript + qpdf + Tesseract) running in
[Open Terminal](https://github.com/open-webui/open-terminal); the LLM only decides *what* to redact.

**Only assumption:** Open WebUI and Open Terminal run on the same server and Open Terminal listens on port 8000.

## Where everything goes

| File in this repo | Goes to | How |
|---|---|---|
| `open-terminal/redact.py` | Open Terminal: `~/.local/share/redact/redact.py` + `redact` command in `~/.local/bin/` | `setup_openterminal.py` (or Docker image) |
| `open-terminal/AGENTS.md` | Open Terminal: `~/AGENTS.md` (Open WebUI injects it into every chat turn) | `setup_openterminal.py` |
| `open-terminal/skills/pdf-redaction/SKILL.md` | Open WebUI: **Workspace → Skills** (id `pdf-redaction`, linked to the model). Optionally also served by the terminal (`--terminal-skill` / `REDACT_TERMINAL_SKILL=1`) - not both, or the model sees it twice | `setup_openwebui.py` or paste |
| `open-webui/tools/pdf_redactor_delivery.py` | Open WebUI: **Workspace → Tools** (id `pdf_redactor_delivery`) | `setup_openwebui.py` or paste |
| `open-webui/functions/pdf_redactor_filter.py` | Open WebUI: **Admin Panel → Functions** (id `pdf_redactor_filter`, filter, enabled) | `setup_openwebui.py` or paste |
| `open-webui/prompts/system-prompt.txt` | Open WebUI: model **PDF Redactor** → System Prompt | `setup_openwebui.py` or paste |
| `open-webui/models/pdf-redactor.json` | Open WebUI: **Workspace → Models → Import** | `setup_openwebui.py` or import |
| Terminal connection | Open WebUI: **Admin Settings → Integrations → Open Terminal** | `setup_openwebui.py` or by hand |
| `open-terminal/docker/` | optional hardened Open Terminal image with everything built in | `docker compose` |
| `testing/` | test campaign: synthetic documents, independent leak checker | see `docs/TESTING.md` |

Every Open WebUI setting is listed with its exact value in [`docs/OPEN_WEBUI_SETTINGS.md`](docs/OPEN_WEBUI_SETTINGS.md).

## Quick start

Requirements: Open WebUI ≥ 0.11 (Open Terminal integration), an Open Terminal on port 8000 (default image, which
has passwordless `sudo`), a tool-capable model in Open WebUI (tested: `qwen3.5:9b` via Ollama, 16k context).

```bash
git clone https://github.com/gorzalniksamuel/openwebui-pdf-redactor && cd openwebui-pdf-redactor

# 1. install the redactor into Open Terminal (dependencies, redact CLI, AGENTS.md, self-test)
python3 open-terminal/setup_openterminal.py --url http://localhost:8000 --key "$OPEN_TERMINAL_API_KEY"

# 2. configure Open WebUI (terminal connection, tool, filter, skill, model). API key: Settings → Account → API Keys
python3 open-webui/setup_openwebui.py --webui http://localhost:3000 --api-key "$OWUI_API_KEY" \
    --terminal-url http://host.docker.internal:8000 --terminal-key "$OPEN_TERMINAL_API_KEY" \
    --base-model qwen3.5:9b
```

`--terminal-url` is how the **Open WebUI server** reaches Open Terminal. Open WebUI in Docker, terminal on the host's
port 8000: `http://host.docker.internal:8000` (needs `extra_hosts: ["host.docker.internal:host-gateway"]`) or
`http://<server-ip>:8000`. Both in the same Docker network: `http://<terminal-container-name>:8000`.

Then: new chat → model **PDF Redactor** → attach a PDF → "Schwärze alle Namen und die IBAN".

To keep the dependencies across Open Terminal container re-creation, add to its environment:

```
OPEN_TERMINAL_PACKAGES=ghostscript qpdf tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-osd
OPEN_TERMINAL_PIP_PACKAGES=pymupdf==1.26.5
```

(the redact files live in the home volume and persist anyway). Or use the hardened image in `open-terminal/docker/`.

Use **one** of the two ways per terminal: the setup script puts `redact` into `~/.local/bin`, which comes first on
`PATH` and would shadow the version built into the Docker image. When switching to the image, delete
`~/.local/bin/redact` and `~/.local/share/redact` in the terminal; when updating via the script, just re-run it.

## How a chat works

1. The attached PDF is written into the terminal (chat uploads = filesystem; Open WebUI does not text-extract it).
2. The model runs `redact extract` (compact page text, OCR text of images/scans, barcodes), decides the targets and
   runs `redact apply` with `--terms`, `--label` (value after a label, e.g. `Auftragsnummer`), `--builtin`
   (EMAIL, PHONE, IBAN, DATE, …) or `--regex`.
3. `apply` redacts, flattens, rewrites, scrubs and **verifies** (8 hard checks). Only a verified file is delivered:
   the filter attaches it to the reply as a download card.
4. Follow-ups ("also the order numbers") re-run apply on the original with all targets so far; every stage is a new
   verified download. An already redacted PDF can be attached and redacted further.

Details: [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md). Test campaign and results: [`docs/TESTING.md`](docs/TESTING.md).

## Privacy notes

- `redact extract` output (page text) is a tool result and stays in the Open WebUI chat history until the chat is
  deleted. Delete the chat after a sensitive document.
- Uploads, outputs and their side files (redaction areas, verification marker, remembered targets for follow-ups)
  in the terminal are deleted after the retention time (default 60 min, `--retention`).
- The terminal connection is shared by everyone allowed to use it: restrict it to a group in Open WebUI.

## License

AGPL-3.0 (PyMuPDF and Ghostscript are AGPL-3.0).
