#!/usr/bin/env python3
"""Install the PDF redactor into an EXISTING Open Terminal through its API (Python standard library only).

  python3 setup_openterminal.py --url http://localhost:8000 --key <OPEN_TERMINAL_API_KEY> [--retention 60]

What it does (idempotent):
  1. checks the terminal and installs the system/Python dependencies if they are missing
     (apt: ghostscript qpdf tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-osd; pip: pymupdf).
     Needs root or passwordless sudo inside the terminal (the default Open Terminal image has it; the slim/alpine
     images do not -> use the Dockerfile in this folder instead).
  2. uploads redact.py to ~/.local/share/redact/, a `redact` command to ~/.local/bin/
  3. writes the agent instructions to ~/AGENTS.md (kept between markers if the file already has other content);
     with --terminal-skill also the skill to ~/.agents/skills/pdf-redaction/SKILL.md (normally the skill is created
     in Open WebUI by setup_openwebui.py)
  4. sets the retention time (~/.config/redact/retention_min): PDFs + side files in the home directory older
     than N minutes are deleted at the start of every redact command (0 = never)
  5. self-test: creates a small PDF, redacts it, verifies it

Files in the home directory survive container restarts (it is a volume in the default setup). apt/pip packages do
not: add them to the container's environment so they are re-installed on start (see README), or re-run this script.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APT = "ghostscript qpdf tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-osd"
PIP = "pymupdf==1.26.5"
BEGIN, END = "<!-- pdf-redactor:begin -->", "<!-- pdf-redactor:end -->"


class Terminal:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    def req(self, method, path, body=None, timeout=60):
        r = urllib.request.Request(self.url + path, method=method,
                                   data=None if body is None else json.dumps(body).encode(),
                                   headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def run(self, cmd, wait=300):
        """Run a command; the API waits at most 300 s per call, longer commands are polled until done."""
        step = min(wait, 300)
        d = self.req("POST", f"/execute?wait={step}", {"command": cmd}, timeout=step + 30)
        out = "".join(e.get("data", "") for e in d.get("output", []) if isinstance(e, dict))
        offset = d.get("next_offset", 0)
        deadline = time.time() + wait
        while d.get("status") == "running" and time.time() < deadline:
            d = self.req("GET", f"/execute/{d['id']}/status?wait={step}&offset={offset}", timeout=step + 30)
            out += "".join(e.get("data", "") for e in d.get("output", []) if isinstance(e, dict))
            offset = d.get("next_offset", offset)
        return d.get("exit_code"), out

    def write(self, path, content):
        self.req("POST", "/files/write", {"path": path, "content": content})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Open Terminal URL, e.g. http://localhost:8000")
    ap.add_argument("--key", default=os.environ.get("OPEN_TERMINAL_API_KEY"), help="Open Terminal API key")
    ap.add_argument("--retention", type=int, default=60, help="minutes until uploads/outputs are deleted (0 = never)")
    ap.add_argument("--skip-deps", action="store_true", help="do not install apt/pip packages")
    ap.add_argument("--terminal-skill", action="store_true",
                    help="also install the skill in the terminal (~/.agents/skills); not needed when setup_openwebui.py "
                         "creates it as a workspace skill - both together list it twice")
    a = ap.parse_args()
    if not a.key:
        sys.exit("--key (or env OPEN_TERMINAL_API_KEY) is required")
    t = Terminal(a.url, a.key)
    try:
        print("terminal:", t.req("GET", "/health"))
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"cannot reach {a.url}: {e}")

    _, home = t.run("printf %s \"$HOME\"")
    home = home.strip() or "/home/user"
    print("home:", home)

    if not a.skip_deps:
        code, out = t.run("command -v gs qpdf tesseract >/dev/null && python3 -c 'import pymupdf' 2>/dev/null "
                          "&& tesseract --list-langs 2>/dev/null | grep -q '^deu' && echo present")
        if "present" in out:
            print("ok    dependencies already installed")
        else:
            code, out = t.run("if [ \"$(id -u)\" = 0 ]; then S=; elif sudo -n true 2>/dev/null; then S=sudo; "
                              "else echo NO_ROOT; exit 3; fi; "
                              f"$S apt-get update -qq && $S apt-get install -y --no-install-recommends {APT} "
                              f"&& (python3 -m pip install --no-cache-dir {PIP} || $S python3 -m pip install "
                              f"--no-cache-dir {PIP} || python3 -m pip install --user --break-system-packages {PIP})",
                              wait=900)
            if code != 0:
                sys.exit("FAILED to install dependencies (no root/sudo in this terminal image?). Use the Dockerfile "
                         "in open-terminal/ or install manually: apt " + APT + "; pip " + PIP + "\n" + out[-1500:])
            print("ok    dependencies installed")

    t.run(f"mkdir -p {home}/.local/share/redact {home}/.local/bin {home}/.agents/skills/pdf-redaction "
          f"{home}/.config/redact")
    t.write(f"{home}/.local/share/redact/redact.py", open(os.path.join(HERE, "redact.py")).read())
    t.write(f"{home}/.local/bin/redact", f"#!/bin/sh\nexec python3 {home}/.local/share/redact/redact.py \"$@\"\n")
    t.run(f"chmod 755 {home}/.local/bin/redact")
    print("ok    redact command installed")

    agents = open(os.path.join(HERE, "AGENTS.md")).read().strip()
    try:
        cur = t.req("GET", f"/files/read?path={home}/AGENTS.md").get("content", "")
    except urllib.error.HTTPError:
        cur = ""
    block = f"{BEGIN}\n{agents}\n{END}"
    if BEGIN in cur and END in cur:
        new = cur[:cur.index(BEGIN)] + block + cur[cur.index(END) + len(END):]
    elif cur.strip():
        new = cur.rstrip() + "\n\n" + block + "\n"
    else:
        new = block + "\n"
    t.write(f"{home}/AGENTS.md", new)
    if a.terminal_skill:  # optional: the skill normally lives in Open WebUI (Workspace -> Skills)
        t.write(f"{home}/.agents/skills/pdf-redaction/SKILL.md",
                open(os.path.join(HERE, "skills", "pdf-redaction", "SKILL.md")).read())
    t.write(f"{home}/.config/redact/retention_min", f"{a.retention}\n")
    print(f"ok    AGENTS.md{', terminal skill' if a.terminal_skill else ''}, retention {a.retention} min")

    test = ("cd $(mktemp -d) && python3 -c \"import pymupdf; d=pymupdf.open(); p=d.new_page(); "
            "p.insert_text((60,80),'Self test Lena Hofmann, IBAN DE89 3704 0044 0532 0130 00'); d.save('t.pdf')\" "
            "&& REDACT_RETENTION_MIN=0 PATH=$HOME/.local/bin:$PATH redact apply t.pdf --terms 'Lena Hofmann' "
            "--builtin IBAN; c=$?; cd /; exit $c")
    code, out = t.run(test, wait=180)
    print(out.strip()[-600:])
    if code != 0 or "VERIFY PASSED" not in out:
        sys.exit("FAILED: self-test")
    print("\nok    self-test passed. Next: python3 ../open-webui/setup_openwebui.py ...")


if __name__ == "__main__":
    main()
