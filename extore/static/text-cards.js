"use strict";

(() => {
  const MAX_BYTES = 2 * 1024 * 1024;
  const MAX_ITEMS = 1000;
  const MAX_LINE = 10000;
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const tr = (cn, en) => window.ExtorePreferences?.resolved.language === "en" ? en : cn;

  function parse(value) {
    if (new TextEncoder().encode(value).byteLength > MAX_BYTES)
      throw new Error(tr("导入内容最多 2 MiB，请拆成几批。", "Import up to 2 MiB at a time."));
    const text = value.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
    const lines = text.split("\n");
    if (text.endsWith("\n")) lines.pop();
    const items = [], seen = new Set();
    let blank = 0, duplicates = 0;
    for (const raw of lines) {
      const line = raw.trim();
      if (!line) { blank++; continue; }
      if (Array.from(line).length > MAX_LINE)
        throw new Error(tr("每行最多 10,000 字符。", "Each line allows up to 10,000 characters."));
      if (/[\u0000-\u0008\u000B-\u001F]/.test(line))
        throw new Error(tr("文本包含不支持的控制字符。", "Remove unsupported control characters."));
      if (seen.has(line)) { duplicates++; continue; }
      seen.add(line); items.push(line);
      if (items.length > MAX_ITEMS)
        throw new Error(tr("每次最多创建 1,000 张卡密，请分批导入。", "Create up to 1,000 codes per import."));
    }
    return { items, lines: lines.length, blank, duplicates, created: items.length };
  }

  function mount({ root, api, endpoint = "/admin", product, variants, isCurrent = () => true, onIssued = () => {}, onBusy = () => {} }) {
    const lifetime = new AbortController();
    let busy = false, disposed = false, generation = 0;
    const current = () => !disposed && root.isConnected !== false && isCurrent();
    const node = (id) => root.querySelector("#" + id);
    const enabled = variants.filter((variant) => variant.enabled !== false);
    root.innerHTML = `<section class="panel"><h3>${tr("导入文本并生成卡密", "Import text and create codes")}</h3><p class="caption">${tr("一行对应一张卡密。忽略空行，本次导入的重复行只创建一张；行内空格会保留。相同文本之后可以再次导入。", "One line creates one code. Blank lines are ignored and duplicate lines in this import are merged. Interior spacing is preserved; the same text can be imported again later.")}</p>
      <form id="text-cards-form"><div class="grid"><div class="field"><label for="text-cards-variant">${tr("制卡规格", "Variant to issue")}</label><select id="text-cards-variant" required ${enabled.length ? "" : "disabled"}>${enabled.map((variant) => `<option value="${escape(variant.id)}">${escape(variant.name)}</option>`).join("")}</select></div><div class="field"><label for="text-cards-file">${tr("导入 UTF-8 文本文件", "Import a UTF-8 text file")}</label><input id="text-cards-file" type="file" accept=".txt,text/plain"><p class="caption">${tr("最多 2 MiB，每次最多 1,000 条，每条最多 10,000 字符。", "Up to 2 MiB, 1,000 items and 10,000 characters per item.")}</p></div></div>
      <div class="field"><label for="text-cards-text">${tr("粘贴交付文本（每行一条）", "Paste delivery text (one item per line)")}</label><textarea id="text-cards-text" rows="8" autocomplete="off" spellcheck="false" required></textarea></div><p id="text-cards-preview" class="caption" role="status" aria-live="polite"></p>
      <div class="grid"><div class="field"><label for="text-cards-label">${tr("批次标签（可选）", "Batch label (optional)")}</label><input id="text-cards-label" maxlength="100"></div><div class="field"><label for="text-cards-expires">${tr("兑换截止时间（可选，本地时间）", "Redemption deadline (optional, local time)")}</label><input id="text-cards-expires" type="datetime-local"></div></div><button id="text-cards-submit" type="submit" class="full" ${enabled.length ? "" : "disabled"}>${tr("生成对应卡密", "Create matching codes")}</button><div id="text-cards-error" class="error" role="alert"></div></form><div id="text-cards-result" class="secret-output"></div></section>`;

    const clear = () => { generation++; node("text-cards-result").innerHTML = ""; };
    const preview = () => {
      node("text-cards-error").textContent = "";
      try {
        const stats = parse(node("text-cards-text").value);
        node("text-cards-preview").textContent = tr(
          `将创建 ${stats.created} 张卡密 · 忽略 ${stats.blank} 个空行 · 合并 ${stats.duplicates} 个重复行`,
          `${stats.created} codes · ${stats.blank} blank lines skipped · ${stats.duplicates} duplicates merged`,
        );
      } catch (error) {
        node("text-cards-preview").textContent = "";
        node("text-cards-error").textContent = error.message;
      }
    };
    root.addEventListener("input", (event) => { if (busy) return; clear(); if (event.target.id === "text-cards-text") preview(); }, { signal: lifetime.signal });
    root.addEventListener("change", (event) => { if (!busy && event.target.id !== "text-cards-file") clear(); }, { signal: lifetime.signal });
    node("text-cards-file").addEventListener("change", async () => {
      const file = node("text-cards-file").files?.[0];
      if (!file || busy) return;
      clear();
      const requestGeneration = generation;
      try {
        if (file.size > MAX_BYTES) throw new Error(tr("文件最多 2 MiB。", "Files may be up to 2 MiB."));
        const buffer = await file.arrayBuffer();
        if (!current() || requestGeneration !== generation) return;
        let value;
        try { value = new TextDecoder("utf-8", { fatal: true }).decode(buffer); }
        catch { throw new Error(tr("请导入 UTF-8 编码的文本文件。", "Choose a UTF-8 text file.")); }
        parse(value);
        node("text-cards-text").value = value;
        preview();
      } catch (error) {
        if (current() && requestGeneration === generation) node("text-cards-error").textContent = error.message;
      } finally {
        if (current()) node("text-cards-file").value = "";
      }
    }, { signal: lifetime.signal });

    node("text-cards-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!current() || busy) return;
      node("text-cards-error").textContent = "";
      try {
        const text = node("text-cards-text").value;
        if (!parse(text).created) throw new Error(tr("至少填写一行非空文本。", "Enter at least one nonempty line."));
        const variant = node("text-cards-variant").value;
        if (!enabled.some((item) => item.id === variant)) throw new Error(tr("请先启用一个规格。", "Enable a variant first."));
        const date = node("text-cards-expires").value;
        const expires = date ? new Date(date).getTime() / 1000 : null;
        if (date && (!Number.isFinite(expires) || expires <= Date.now() / 1000)) throw new Error(tr("兑换截止时间必须晚于当前时间。", "Choose a future redemption deadline."));
        busy = true; clear(); onBusy(true);
        root.querySelectorAll("input, select, textarea, button").forEach((element) => { element.disabled = true; });
        const result = await api(`${endpoint}/cards/import-text`, { product_id: product.id, variant_id: variant, text, label: node("text-cards-label").value.trim(), expires }, "POST", { signal: lifetime.signal });
        if (!current()) return;
        showResult(result);
        // Clear the imported originals after issuance; the current result below
        // is the only copy the page retains and it is never written to storage.
        node("text-cards-text").value = "";
        node("text-cards-preview").textContent = "";
        await onIssued(result);
      } catch (error) {
        if (current() && error.name !== "AbortError") node("text-cards-error").textContent = error.message || tr("导入失败，请重试。", "Import failed. Try again.");
      } finally {
        busy = false;
        if (current()) {
          onBusy(false);
          root.querySelectorAll("input, select, textarea, button").forEach((element) => { element.disabled = false; });
          node("text-cards-variant").disabled = !enabled.length;
          node("text-cards-submit").disabled = !enabled.length;
        }
      }
    }, { signal: lifetime.signal });

    function showResult(result) {
      const codes = Array.isArray(result.codes) ? result.codes : [];
      const items = Array.isArray(result.items) ? result.items : [];
      const issued = generation;
      const valid = () => current() && issued === generation;
      const mapping = JSON.stringify({ schema: "extore.text-cards.v1", product_id: product.id, variant_id: node("text-cards-variant").value, batch_id: result.batch_id, items }, null, 2);
      node("text-cards-result").innerHTML = `<h4>${tr(`本次创建 ${codes.length} 张卡密`, `${codes.length} codes created`)}</h4><p class="caption">${tr(`忽略 ${result.stats?.blank || 0} 个空行，合并 ${result.stats?.duplicates || 0} 个重复行。原文仅本次显示，请立即保存。`, `${result.stats?.blank || 0} blank lines skipped; ${result.stats?.duplicates || 0} duplicates merged. Originals are shown only now. Save them before leaving.`)}</p><label for="text-cards-generated">${tr("本次生成的卡密", "Generated codes")}</label><textarea id="text-cards-generated" readonly rows="5" spellcheck="false"></textarea><div class="actions"><button id="text-cards-copy" type="button" class="secondary">${tr("复制全部卡密", "Copy all codes")}</button><button id="text-cards-download" type="button" class="secondary">${tr("下载卡密", "Download codes")}</button><button id="text-cards-mapping" type="button" class="secondary">${tr("下载卡密与文本对应表", "Download code/text mapping")}</button></div><p id="text-cards-copy-status" class="caption" role="status" aria-live="polite"></p>`;
      node("text-cards-generated").value = codes.join("\n");
      node("text-cards-copy").addEventListener("click", async () => {
        if (!valid()) return;
        try {
          const written = window.ExtoreClipboard?.writeText ? await window.ExtoreClipboard.writeText(codes.join("\n")) : (await navigator.clipboard.writeText(codes.join("\n")), true);
          if (!written) throw new Error("copy");
          if (valid()) node("text-cards-copy-status").textContent = tr("已复制全部卡密。", "All codes copied.");
        } catch {
          if (!valid()) return;
          node("text-cards-generated").focus(); node("text-cards-generated").select();
          node("text-cards-copy-status").textContent = tr("浏览器未允许复制，已选中卡密，请手动复制。", "Copy was blocked. The codes are selected for manual copying.");
        }
      }, { signal: lifetime.signal });
      const download = (content, filename, type) => {
        if (!valid()) return;
        const url = URL.createObjectURL(new Blob([content], { type }));
        const link = document.createElement("a"); link.href = url; link.download = filename;
        link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      };
      node("text-cards-download").addEventListener("click", () => download(codes.join("\n") + "\n", "extore-codes.txt", "text/plain;charset=utf-8"), { signal: lifetime.signal });
      node("text-cards-mapping").addEventListener("click", () => download(mapping, "extore-code-text.json", "application/json"), { signal: lifetime.signal });
    }
    preview();
    return { dispose() { disposed = true; generation++; lifetime.abort(); root.innerHTML = ""; } };
  }
  window.ExtoreTextCards = { mount, parse, MAX_BYTES, MAX_ITEMS, MAX_LINE };
})();
