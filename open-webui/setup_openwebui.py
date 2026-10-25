#!/usr/bin/env python3
"""Configure Open WebUI for the PDF redactor through its admin API (Python standard library only).

What it sets up (idempotent, safe to re-run):
  1. Open Terminal connection  (Admin Settings -> Integrations -> Open Terminal), chat uploads = filesystem
  2. Tool      pdf_redactor_delivery  (Workspace -> Tools) + valves (terminal URL, key)
  3. Function  pdf_redactor_filter    (Admin Panel -> Functions), activated + valves
  4. Model     pdf-redactor           (Workspace -> Models): base model, system prompt, tool, filter, terminal

Usage:
  python3 setup_openwebui.py --webui http://localhost:3000 --api-key sk-... \\
      --terminal-url http://host.docker.internal:8000 --terminal-key <OPEN_TERMINAL_API_KEY> \\
      [--base-model qwen3.5:9b] [--terminal-id pdf-redactor] [--num-ctx 16384]
  (instead of --api-key: --email admin@example.com --password ...; or env OWUI_API_KEY / OWUI_PASSWORD)

--terminal-url is the address under which the Open WebUI SERVER (not your browser) reaches Open Terminal.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def call(base, method, path, token=None, body=None):
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


def must(status, data, what):
    if status != 200:
        sys.exit(f"FAILED: {what}: HTTP {status}: {data}")
    print(f"ok    {what}")
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--webui", required=True, help="Open WebUI base URL, e.g. http://localhost:3000")
    ap.add_argument("--api-key", default=os.environ.get("OWUI_API_KEY"), help="admin API key (or env OWUI_API_KEY)")
    ap.add_argument("--email")
    ap.add_argument("--password", default=os.environ.get("OWUI_PASSWORD"))
    ap.add_argument("--terminal-url", required=True, help="Open Terminal URL as seen from the Open WebUI server")
    ap.add_argument("--terminal-key", default=os.environ.get("OPEN_TERMINAL_API_KEY"), help="Open Terminal API key")
    ap.add_argument("--terminal-id", default="pdf-redactor")
    ap.add_argument("--terminal-name", default="PDF Redactor")
    ap.add_argument("--base-model", default="qwen3.5:9b", help="tool-capable model id known to Open WebUI")
    ap.add_argument("--num-ctx", type=int, default=16384, help="context window (>= 16384 recommended)")
    a = ap.parse_args()
    if not a.terminal_key:
        sys.exit("--terminal-key (or env OPEN_TERMINAL_API_KEY) is required")

    token = a.api_key
    if not token:
        if not (a.email and a.password):
            sys.exit("give --api-key, or --email and --password")
        token = must(*call(a.webui, "POST", "/api/v1/auths/signin", body={"email": a.email, "password": a.password}),
                     "sign in")["token"]

    # 1. terminal connection
    conn = {"id": a.terminal_id, "name": a.terminal_name, "enabled": True, "url": a.terminal_url.rstrip("/"),
            "path": "/openapi.json", "key": a.terminal_key, "auth_type": "bearer", "forward_cookies": False,
            "config": {"chat_uploads": "filesystem"}}
    must(*call(a.webui, "POST", "/api/v1/configs/terminal_servers/verify", token, conn),
         f"Open WebUI server reaches Open Terminal at {conn['url']}")
    st, cur = call(a.webui, "GET", "/api/v1/configs/terminal_servers", token)
    existing = (cur or {}).get("TERMINAL_SERVER_CONNECTIONS") or [] if st == 200 else []
    conns = [c for c in existing if c.get("id") != a.terminal_id] + [conn]
    must(*call(a.webui, "POST", "/api/v1/configs/terminal_servers", token, {"TERMINAL_SERVER_CONNECTIONS": conns}),
         f"terminal connection '{a.terminal_id}' (chat uploads: filesystem)")

    valves = {"terminal_url": conn["url"], "terminal_api_key": a.terminal_key}

    # 2. tool
    tool = {"id": "pdf_redactor_delivery", "name": "PDF Redactor delivery",
            "content": open(os.path.join(HERE, "tools", "pdf_redactor_delivery.py")).read(),
            "meta": {"description": "Attaches a verified redacted PDF from the terminal to the chat."}}
    st, _ = call(a.webui, "GET", "/api/v1/tools/id/pdf_redactor_delivery", token)
    must(*call(a.webui, "POST", "/api/v1/tools/id/pdf_redactor_delivery/update" if st == 200 else "/api/v1/tools/create",
               token, tool), "tool pdf_redactor_delivery")
    must(*call(a.webui, "POST", "/api/v1/tools/id/pdf_redactor_delivery/valves/update", token, valves), "tool valves")

    # 3. filter function
    fn = {"id": "pdf_redactor_filter", "name": "PDF Redactor filter",
          "content": open(os.path.join(HERE, "functions", "pdf_redactor_filter.py")).read(),
          "meta": {"description": "Delivery tool always on, compact history, automatic download of verified PDFs."}}
    st, _ = call(a.webui, "GET", "/api/v1/functions/id/pdf_redactor_filter", token)
    must(*call(a.webui, "POST", "/api/v1/functions/id/pdf_redactor_filter/update" if st == 200
               else "/api/v1/functions/create", token, fn), "function pdf_redactor_filter")
    st, f = call(a.webui, "GET", "/api/v1/functions/id/pdf_redactor_filter", token)
    if st == 200 and not f.get("is_active"):
        must(*call(a.webui, "POST", "/api/v1/functions/id/pdf_redactor_filter/toggle", token), "function activated")
    must(*call(a.webui, "POST", "/api/v1/functions/id/pdf_redactor_filter/valves/update", token, valves),
         "function valves")

    # 4. model
    model = json.load(open(os.path.join(HERE, "models", "pdf-redactor.json")))[0]
    model["base_model_id"] = a.base_model
    model["params"]["system"] = open(os.path.join(HERE, "prompts", "system-prompt.txt")).read().strip()
    model["params"]["num_ctx"] = a.num_ctx
    model["meta"]["terminalId"] = a.terminal_id
    must(*call(a.webui, "POST", "/api/v1/models/import", token, {"models": [model]}),
         f"model pdf-redactor (base {a.base_model})")
    print("\nDone. Start a new chat with the model 'PDF Redactor', attach a PDF and say what to black out.")


if __name__ == "__main__":
    main()
