# Lean PDF redaction toolkit on top of the official Open Terminal slim image.
#   python3 + PyMuPDF + Ghostscript + qpdf + Tesseract (deu/eng). Tesseract only reads the embedded images of
#   text pages (fast); image-only (scanned) pages are still reported as "needs OCR" and handed to the full app.
#
# The slim image ships without pip, so Python packages are built in a throwaway stage
# (same Python 3.12 base as the slim image) and copied in.
ARG OT_IMAGE=ghcr.io/open-webui/open-terminal:slim@sha256:9297b439b41f6622ba0b0b6d0e2392c111324914fcf50a40379bd5364edd48c2

FROM python:3.12.13-slim AS pydeps
RUN pip install --no-cache-dir --prefix=/install "pymupdf==1.26.5" "pytest==8.4.2"

FROM ${OT_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends ghostscript qpdf \
       tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY --from=pydeps /install /usr/local
COPY redact.py /opt/redact/redact.py
COPY tests/ /opt/redact/tests/
COPY AGENTS.md docker/entrypoint.sh /opt/redact/
COPY skills/ /opt/redact/skills/
RUN printf '#!/bin/sh\nexec python3 /opt/redact/redact.py "$@"\n' > /usr/local/bin/redact \
    && chmod 755 /usr/local/bin/redact /opt/redact/entrypoint.sh \
    && python3 -c "import pymupdf; print('PyMuPDF', pymupdf.VersionBind)" \
    && gs --version && qpdf --version | head -1 && tesseract --list-langs

# Runs entirely as the unprivileged "user" (uid 1000): no root entrypoint, no capabilities needed.
# Egress is blocked by the docker network (internal: true), not by iptables inside the container.
USER 1000:1000
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/redact/entrypoint.sh"]
CMD ["run"]
