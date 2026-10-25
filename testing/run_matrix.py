#!/usr/bin/env python3
"""Test campaign: redact every case with `redact apply`, then check the output with the INDEPENDENT leak checker
(check.py: poppler, qpdf, tesseract, Pillow - no PyMuPDF), which looks for every secret in the text layer, raw and
decompressed bytes, hex strings, metadata, every embedded image (OCR) and the rendered pages (OCR, also rotated,
high-contrast and mirrored) and checks that the words that must stay are still readable.

  docker build -t redact-checker -f testing/Dockerfile.checker testing
  python3 testing/run_matrix.py [--redact "python3 open-terminal/redact.py"] [case ...]

The redact command needs pymupdf, gs, qpdf, tesseract (e.g. run this inside the Open Terminal, or pass
--redact "docker run --rm -v $PWD/testing/work:/w -w /w --entrypoint redact pdf-redactor-terminal").
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cases import CASES  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--redact", default=f"python3 {os.path.join(HERE, '..', 'open-terminal', 'redact.py')}")
ap.add_argument("--checker", default="docker run --rm --network none -v {work}:/w:ro -v {here}:/t:ro redact-checker "
                                     "python3 /t/check.py")
ap.add_argument("cases", nargs="*")
a = ap.parse_args()

work = os.path.join(HERE, "work")
for d in ("fx", "syn", "out"):
    os.makedirs(os.path.join(work, d), exist_ok=True)
for f in os.listdir(os.path.join(HERE, "fixtures")):
    shutil.copy(os.path.join(HERE, "fixtures", f), os.path.join(work, "fx", f))
redact = shlex.split(a.redact)
subprocess.run(["python3", os.path.join(HERE, "gen_synth.py"), os.path.join(work, "syn")], check=False)
docker_redact = "docker" in redact[0]
checker = shlex.split(a.checker.format(work=work, here=HERE))
results, failed = [], 0
for cid, src, args, secrets, keep in CASES:
    if a.cases and cid not in a.cases:
        continue
    out = f"out/{cid}_redacted.pdf"
    t0 = time.time()
    p = subprocess.run([*redact, "apply", src if docker_redact else os.path.join(work, src), "-o",
                        out if docker_redact else os.path.join(work, out), *args],
                       capture_output=True, text=True, cwd=work, env={**os.environ, "REDACT_RETENTION_MIN": "0"})
    r = {"case": cid, "apply_exit": p.returncode, "seconds": round(time.time() - t0, 1)}
    if p.returncode == 0:
        c = subprocess.run([*checker, f"/w/{out}", *sum([["--secret", s] for s in secrets], []),
                            *sum([["--keep", k] for k in keep], [])], capture_output=True, text=True)
        try:
            r.update(json.loads(c.stdout))
        except ValueError:
            r["checker_error"] = c.stderr[-500:]
    r["apply_tail"] = (p.stdout + p.stderr)[-900:]
    ok = p.returncode == 0 and r.get("ok") and not r.get("keep_missing")
    failed += not ok
    results.append(r)
    print("OK " if ok else "!! ", cid, f"{r['seconds']}s", "leaks=", r.get("leaks"), "structure=", r.get("structure"),
          "keep_missing=", r.get("keep_missing"), flush=True)
json.dump(results, open(os.path.join(work, "results.json"), "w"), ensure_ascii=False, indent=1)
print(f"\n{len(results) - failed}/{len(results)} cases clean; details in testing/work/results.json")
sys.exit(1 if failed else 0)
