"""
title: PDF Redactor filter
description: For the PDF Redactor model - enables the delivery tool, keeps follow-ups inside the context and attaches verified redacted PDFs to the reply automatically.
version: 1.2.0
"""

# Open WebUI filter function (Admin Panel -> Functions, id pdf_redactor_filter), listed in the model's filterIds.
# inlet:
#  1. Tool selection is sent by the browser and can be stale (page loaded before the model got the tool); the
#     delivery tool is added server-side to every request of this model.
#  2. Open WebUI resends every earlier tool result with each follow-up. A page extract alone is several thousand
#     tokens, so the 16k context filled up after one turn and follow-ups came back empty. Tool results from earlier
#     turns (before the last user message) are cut to head + tail; tool calls (commands, terms) stay intact.
# outlet:
#  3. The 9b model does not reliably call the delivery tool. After every reply, each PDF attached in this chat is
#     checked for a verified output (<name>_redacted.pdf + .verified sha256 + .redact.json source = that PDF); a
#     verified output not yet delivered in this chat is attached to the reply as a download card.

import hashlib
import io
import json
import posixpath
import uuid

import aiohttp
from pydantic import BaseModel, Field

from open_webui.models.chats import Chats
from open_webui.models.files import FileForm, Files
from open_webui.storage.provider import Storage

MAX_BYTES = 100 * 1024 * 1024


def attached_pdfs(chat) -> list[str]:
    out = []
    for m in ((chat.chat if chat else {}) or {}).get("history", {}).get("messages", {}).values():
        if m.get("role") == "user":
            for f in m.get("files") or []:
                p = f.get("path") or f.get("id")
                if f.get("type") == "filesystem" and p and p.lower().endswith(".pdf") and p not in out:
                    out.append(p)
    return out


def delivered_hashes(chat) -> set:
    out = set()
    for m in ((chat.chat if chat else {}) or {}).get("history", {}).get("messages", {}).values():
        for f in m.get("files") or []:
            if f.get("sha256"):
                out.add(f["sha256"])
    return out


class Filter:
    class Valves(BaseModel):
        tool_ids: list[str] = Field(["pdf_redactor_delivery"], description="Tool ids that are always enabled")
        old_tool_output_chars: int = Field(700, description="Max chars kept of a tool result from an earlier turn")
        old_extract_chars: int = Field(2500, description="Chars kept of a `redact extract` result from an earlier turn")
        terminal_url: str = Field("", description="Open Terminal base URL as seen from the Open WebUI server, e.g. http://host.docker.internal:8000")
        terminal_api_key: str = Field("", description="OPEN_TERMINAL_API_KEY of the PDF Redactor terminal")

    def __init__(self):
        self.valves = self.Valves()

    def inlet(self, body: dict, __user__: dict = None) -> dict:
        ids = list(body.get("tool_ids") or [])
        body["tool_ids"] = ids + [t for t in self.valves.tool_ids if t not in ids]

        messages = body.get("messages") or []
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        keep = max(200, self.valves.old_tool_output_chars)
        for m in messages[:last_user]:
            c = m.get("content")
            if m.get("role") != "tool" or not isinstance(c, str):
                continue
            if "page(s)" in c[:400] and "=== page" in c:
                # a `redact extract` result: keep its beginning (names, numbers of page 1) for follow-up stages
                head = self.valves.old_extract_chars
                if len(c) > head + 300:
                    m["content"] = (f"{c[:head]}\n[... rest of the extract shortened; run redact extract again for "
                                    f"other pages ...]")
                continue
            if len(c) > keep:
                half = keep // 2
                m["content"] = (f"{c[:half]}\n[... older tool output shortened, {len(c) - keep} chars removed; "
                                f"re-run the command if you need it again ...]\n{c[-half:]}")
        return body

    async def outlet(self, body: dict, __user__: dict = None, __metadata__: dict = None,
                     __event_emitter__=None) -> dict:
        meta = __metadata__ or {}
        chat_id, message_id = meta.get("chat_id"), meta.get("message_id")
        if not chat_id or not message_id or str(chat_id).startswith(("local:", "channel:")) \
                or not self.valves.terminal_api_key or not self.valves.terminal_url:
            return body
        try:
            await self._deliver(chat_id, message_id, (__user__ or {}).get("id"), __event_emitter__)
        except Exception as e:  # never break the chat because of the delivery
            print(f"pdf_redactor outlet: {e}")
        return body

    async def _deliver(self, chat_id, message_id, user_id, emit):
        chat = await Chats.get_chat_by_id(chat_id)
        sources = attached_pdfs(chat)
        if not sources:
            return
        done = delivered_hashes(chat)
        headers = {"Authorization": f"Bearer {self.valves.terminal_api_key}"}
        url = self.valves.terminal_url.rstrip("/") + "/files/view"
        new_files = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async def fetch(p):
                async with s.get(url, params={"path": p}, headers=headers) as r:
                    if r.status != 200:
                        return None
                    return await r.read()

            for src in sources:
                out = posixpath.splitext(src)[0] + "_redacted.pdf"
                marker = await fetch(out + ".verified")
                sidecar = await fetch(out + ".redact.json")
                if not marker or not sidecar:
                    continue
                try:
                    if json.loads(sidecar).get("source") != src:
                        continue
                except ValueError:
                    continue
                want = marker.decode(errors="replace").strip()
                if want in done:
                    continue
                pdf = await fetch(out)
                if not pdf or len(pdf) > MAX_BYTES or hashlib.sha256(pdf).hexdigest() != want:
                    continue
                new_files.append(await self._store(pdf, posixpath.basename(out), want, user_id))
        if not new_files:
            return
        saved = await Chats.add_message_files_by_id_and_message_id(chat_id, message_id, new_files)
        if emit:
            await emit({"type": "chat:message:files", "data": {"files": saved if saved is not None else new_files}})

    @staticmethod
    async def _store(pdf: bytes, name: str, digest: str, user_id: str) -> dict:
        file_id = str(uuid.uuid4())
        contents, stored = Storage.upload_file(io.BytesIO(pdf), f"{file_id}_{name}",
                                               {"OpenWebUI-User-Id": user_id or "", "OpenWebUI-File-Id": file_id})
        item = await Files.insert_new_file(user_id, FileForm(
            id=file_id, hash=digest, filename=name, path=stored, data={},
            meta={"name": name, "content_type": "application/pdf", "size": len(contents), "file_hash": digest},
        ))
        return {"type": "file", "id": item.id, "url": item.id, "name": name, "size": len(contents),
                "content_type": "application/pdf", "status": "uploaded", "sha256": digest}
