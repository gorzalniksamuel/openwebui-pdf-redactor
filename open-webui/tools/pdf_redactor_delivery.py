"""
title: PDF Redactor delivery
description: Attaches a verified redacted PDF from the PDF Redactor terminal to the chat as a download card.
version: 1.1.0
"""

# Open WebUI tool (Workspace -> Tools, id pdf_redactor_delivery), attached to the "PDF Redactor" model.
# Fetches /home/user/*_redacted.pdf from the terminal (hidden /files/view endpoint), checks the sha256 against the
# *.verified marker that only a PASSED `redact verify` writes, checks that the redacted file was made from a PDF
# attached in THIS chat (source recorded in *.redact.json), stores it as an Open WebUI file (no RAG processing)
# and attaches it to the current assistant message.

import hashlib
import io
import json
import os
import posixpath
import uuid

import aiohttp
from pydantic import BaseModel, Field

from open_webui.models.chats import Chats
from open_webui.models.files import FileForm, Files
from open_webui.storage.provider import Storage

HOME = "/home/user"
MAX_BYTES = 100 * 1024 * 1024


class Tools:
    class Valves(BaseModel):
        terminal_url: str = Field("", description="Open Terminal base URL as seen from the Open WebUI server, e.g. http://host.docker.internal:8000")
        terminal_api_key: str = Field("", description="OPEN_TERMINAL_API_KEY of the PDF Redactor terminal")

    def __init__(self):
        self.valves = self.Valves()

    async def deliver_redacted_pdf(self, path: str, __user__: dict = None, __metadata__: dict = None,
                                   __event_emitter__=None) -> str:
        """
        Attach a redacted PDF from the terminal to the chat as a download card for the user.
        Only works for a *_redacted.pdf that passed `redact verify` (exactly this file, unchanged since).
        :param path: Absolute path of the redacted PDF in the terminal, e.g. /home/user/letter_redacted.pdf
        """
        path = posixpath.normpath(path if path.startswith("/") else posixpath.join(HOME, path))
        name = posixpath.basename(path)
        if not path.startswith(HOME + "/") or not name.endswith("_redacted.pdf"):
            return f"Refused: only {HOME}/*_redacted.pdf files can be delivered."

        if not self.valves.terminal_url or not self.valves.terminal_api_key:
            return "Not configured: set the tool valves terminal_url and terminal_api_key (Workspace -> Tools)."
        headers = {"Authorization": f"Bearer {self.valves.terminal_api_key}"}
        url = self.valves.terminal_url.rstrip("/") + "/files/view"
        meta = __metadata__ or {}
        chat_id, message_id = meta.get("chat_id"), meta.get("message_id")
        saved_chat = bool(chat_id) and not str(chat_id).startswith(("local:", "channel:"))
        attached = set()
        if saved_chat:
            chat = await Chats.get_chat_by_id(chat_id)
            for m in ((chat.chat if chat else {}) or {}).get("history", {}).get("messages", {}).values():
                if m.get("role") == "user":
                    attached.update(f.get("path") or f.get("id") for f in m.get("files") or []
                                    if f.get("type") == "filesystem")
            attached.discard(None)

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async def fetch(p):
                async with s.get(url, params={"path": p}, headers=headers) as r:
                    if r.status == 404:
                        return None
                    r.raise_for_status()
                    return await r.read()

            pdf = await fetch(path)
            if pdf is None:
                return f"Not found: {path}"
            if len(pdf) > MAX_BYTES:
                return "Refused: file too large."
            marker = await fetch(path + ".verified")
            sidecar = await fetch(path + ".redact.json")

        if attached:
            try:
                source = json.loads(sidecar or b"{}").get("source")
            except ValueError:
                source = None
            if source not in attached:
                expected = ", ".join(sorted(posixpath.splitext(a)[0] + "_redacted.pdf" for a in attached))
                return (f"Refused: {name} was not made from a PDF attached in this chat. The attached file(s): "
                        f"{', '.join(sorted(attached))}. Run the workflow (extract, apply, verify) on that file and "
                        f"deliver its output ({expected}).")

        digest = hashlib.sha256(pdf).hexdigest()
        if marker is None or marker.decode(errors="replace").strip() != digest:
            return ("Refused: this file has no PASSED verification. Run `redact verify` on it with the same "
                    "targets first; deliver only after VERIFY PASSED.")

        user_id = (__user__ or {}).get("id")
        file_id = str(uuid.uuid4())
        tags = {"OpenWebUI-User-Id": user_id or "", "OpenWebUI-File-Id": file_id}
        contents, stored = Storage.upload_file(io.BytesIO(pdf), f"{file_id}_{name}", tags)
        item = await Files.insert_new_file(user_id, FileForm(
            id=file_id, hash=digest, filename=name, path=stored, data={},
            meta={"name": name, "content_type": "application/pdf", "size": len(contents), "file_hash": digest},
        ))
        entry = {"type": "file", "id": item.id, "url": item.id, "name": name, "size": len(contents),
                 "content_type": "application/pdf", "status": "uploaded", "sha256": digest}

        files = [entry]
        if saved_chat and message_id:
            saved = await Chats.add_message_files_by_id_and_message_id(chat_id, message_id, [entry])
            if saved is not None:
                files = saved
        if __event_emitter__:
            await __event_emitter__({"type": "chat:message:files", "data": {"files": files}})
        return (f"Delivered: {name} ({len(contents) // 1024} KB) is attached to your reply as a download card. "
                "Do not print a link or path; just tell the user it is ready above.")
