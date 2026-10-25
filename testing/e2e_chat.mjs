// Real Open WebUI chats with several turns each (Playwright). Saves every PDF delivered as a download card.
//   OWUI_URL=http://localhost:3000 OWUI_EMAIL=... OWUI_PW=... node e2e_chat.mjs scenarios.json outdir [repeat]
// scenarios.json: [{"id": "A", "file": "/path/doc.pdf" | "@<file saved earlier in outdir>", "turns": ["...", "..."]}]
// Writes outdir/e2e_results.json (per turn: reply, seconds, prompt tokens, delivered files). Needs `npm i playwright`.
import { chromium } from 'playwright';
import fs from 'fs';

const B = process.env.OWUI_URL || 'http://localhost:3000';
const MODEL = process.env.OWUI_MODEL || 'pdf-redactor';
const [scenFile, outDir, repeat = '1'] = process.argv.slice(2);
const scenarios = JSON.parse(fs.readFileSync(scenFile, 'utf8'));
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1400, height: 1000 } })).newPage();
await page.goto(B + '/auth');
await page.fill('input[type=email]', process.env.OWUI_EMAIL);
await page.fill('input[type=password]', process.env.OWUI_PW);
await page.keyboard.press('Enter');
await page.waitForURL(u => !u.pathname.startsWith('/auth'), { timeout: 30000 });
const token = await page.evaluate(() => localStorage.token);
const api = async path => (await fetch(B + path, { headers: { Authorization: `Bearer ${token}` } })).json();

const results = [];
for (let run = 1; run <= Number(repeat); run++) {
  for (const sc of scenarios) {
    const id = Number(repeat) > 1 ? `${sc.id}${run}` : sc.id;
    const file = sc.file.startsWith('@') ? `${outDir}/${sc.file.slice(1).replace('{run}', run)}` : sc.file;
    if (!fs.existsSync(file)) { console.log(id, 'SKIP: input missing', file); continue; }
    await page.goto(`${B}/?models=${MODEL}`);
    await page.waitForTimeout(4000);
    const ok = page.getByText("Okay, Let's Go!");
    if (await ok.count()) { await ok.click(); await page.waitForTimeout(800); }
    await page.setInputFiles('input[type=file]', file);
    await page.waitForTimeout(5000);
    const box = page.locator('#chat-input');
    const turns = [];
    let chatId = null;
    for (const [n, msg] of sc.turns.entries()) {
      const t0 = Date.now();
      await box.click(); await box.type(msg); await page.keyboard.press('Enter');
      for (let i = 0; i < 100; i++) {
        await page.waitForTimeout(4000);
        if (i > 2 && !(await page.locator('#stop-response-button, button[aria-label*="Stop"]').count())) break;
      }
      await page.waitForTimeout(3000);  // outlet filter attaches the file after the reply
      chatId = chatId || page.url().split('/c/')[1] || (await api('/api/v1/chats/'))[0].id;
      const chat = await api(`/api/v1/chats/${chatId}`);
      const h = chat.chat.history;
      const last = h.messages[h.currentId];
      const saved = [];
      for (const f of (last.files || []).filter(f => f.type === 'file')) {
        const r = await fetch(`${B}/api/v1/files/${f.id}/content`, { headers: { Authorization: `Bearer ${token}` } });
        const name = `${id}_t${n + 1}.pdf`;
        fs.writeFileSync(`${outDir}/${name}`, Buffer.from(await r.arrayBuffer()));
        saved.push(name);
      }
      turns.push({ msg, seconds: Math.round((Date.now() - t0) / 1000), reply: (last.content || '').slice(-700),
                   prompt_tokens: last.usage?.prompt_tokens ?? null, delivered: saved });
      console.log(id, `turn ${n + 1}`, `${turns.at(-1).seconds}s`, 'delivered', saved, 'tokens', last.usage?.prompt_tokens);
    }
    results.push({ id, scenario: sc.id, run, chat_id: chatId, turns });
    fs.writeFileSync(`${outDir}/e2e_results.json`, JSON.stringify(results, null, 1));
  }
}
await browser.close();
