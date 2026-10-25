# Open WebUI settings (manual setup)

`open-webui/setup_openwebui.py` sets all of this through the API. This page lists every setting so it can be done by
hand or checked. Tested with Open WebUI 0.11.4 and Open Terminal 0.x (single-user).

## 0. Prerequisites

| What | Value |
|---|---|
| Open Terminal | reachable from the Open WebUI **server** (e.g. `http://host.docker.internal:8000`), API key set (`OPEN_TERMINAL_API_KEY`) |
| Redactor installed in the terminal | `python3 open-terminal/setup_openterminal.py --url … --key …` (self-test must pass) |
| Model | a tool-capable model in Open WebUI. Tested: `qwen3.5:9b` (Ollama). Larger models follow the workflow more reliably |
| Model backend reachable | Open WebUI must reach its model backend (e.g. Ollama); check that the model picker lists models |

## 1. Terminal connection

**Admin Settings → Integrations → Open Terminal → +** (not "Tool Servers")

| Field | Value |
|---|---|
| URL | Open Terminal URL as seen from the Open WebUI server, e.g. `http://host.docker.internal:8000` |
| Auth | Bearer |
| API Key | the terminal's `OPEN_TERMINAL_API_KEY` |
| ID | `pdf-redactor` (the model refers to it as `terminalId`) |
| Name | `PDF Redactor` |
| **Chat Uploads** | **Filesystem**: attached files are written into the terminal and NOT text-extracted by Open WebUI (speed, privacy; the filter relies on it) |
| Working directory context, user shell tools | defaults |
| Forward cookies | off |
| Access | restrict to a group: everyone allowed to use the connection shares the same terminal home directory |

Save → the connection must show as connected.

## 2. Tool `pdf_redactor_delivery`

**Workspace → Tools → +** → paste `open-webui/tools/pdf_redactor_delivery.py`

| Field | Value |
|---|---|
| ID | `pdf_redactor_delivery` |
| Name | `PDF Redactor delivery` |
| Valves (gear icon) | `terminal_url` = same URL as the connection; `terminal_api_key` = terminal API key |

What it does: on request of the model, fetches `/home/user/<name>_redacted.pdf` from the terminal, refuses unless
`<name>_redacted.pdf.verified` (written only by a passing verification) matches the file's SHA-256 and the file was
made from a PDF attached in this chat, stores it as an Open WebUI file (no RAG processing) and attaches it to the
reply as a download card.

## 3. Function `pdf_redactor_filter`

**Admin Panel → Functions → +** → paste `open-webui/functions/pdf_redactor_filter.py`

| Field | Value |
|---|---|
| ID | `pdf_redactor_filter` |
| Type | Filter (detected from the code) |
| Enabled | **on** (toggle) |
| Global | **off** (it is attached to the model in step 4) |
| Valves | `terminal_url`, `terminal_api_key` as above; `tool_ids` = `["pdf_redactor_delivery"]`; `old_tool_output_chars` = `700` |

What it does:
- inlet: always enables the delivery tool (the browser's tool selection can be stale) and shortens tool results of
  earlier turns to 700 characters, so follow-ups fit into a 16k context (without it, follow-ups come back empty).
- outlet: after every reply, attaches each verified, not yet delivered `<attached file>_redacted.pdf` of this chat
  as a download card (the model does not have to remember to deliver).

## 4. Model `PDF Redactor`

**Workspace → Models → Import** → `open-webui/models/pdf-redactor.json`, then open it and check:

| Field | Value |
|---|---|
| Model ID / Name | `pdf-redactor` / `PDF Redactor` |
| Base model | your tool-capable model (file default: `qwen3.5:9b`) |
| System prompt | `open-webui/prompts/system-prompt.txt` (short: the full instructions come from the terminal's `AGENTS.md`) |
| Advanced params | Function Calling **Native**, Think **off** (`think: false`), Temperature `0.2`, Context length (`num_ctx`) **16384** |
| Capabilities | File upload **on**, Vision off, Citations off, Terminal on |
| Tools | **PDF Redactor delivery** ticked |
| Filters | **PDF Redactor filter** ticked |
| Terminal | **PDF Redactor** (the connection from step 1) |
| Visibility | as you like (public or a group) |

`num_ctx`: 16384 is enough because the filter trims history and `redact extract` caps its output (about 9,000
characters). If several apps share the same Ollama model, keep their context length equal to avoid reloads.

## 5. Use

New chat → model **PDF Redactor** (the terminal is preselected) → attach a PDF → e.g.
"Schwärze alle Vornamen", "redact all names, the IBAN and e-mails", follow-up "zusätzlich alle Auftragsnummern".
The redacted PDF appears as a download card under the reply. If no card appears, the file is also in the terminal
file browser (sidebar): `<name>_redacted.pdf`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Model searches `/app`, "no PDF found" | terminal working directory is not writable: Open Terminal must run in `/home/user` (default image does) |
| `Tool "deliver_redacted_pdf" not found` | filter not attached/enabled; or tool id differs from the filter valve `tool_ids` |
| Follow-up answers are empty | filter missing (history overflows the context) or `num_ctx` < 16384 |
| No download card | filter valves `terminal_url`/`terminal_api_key` not set; verification failed (see the apply output) |
| `verify` FAILED | the targets did not cover every occurrence; the file is withheld on purpose. Ask for the missing item |
| Connection "verify" fails in setup | wrong `--terminal-url` from the Open WebUI server's point of view (Docker networking) |
