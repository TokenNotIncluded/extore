"use strict";

(() => {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const text = (value, locale = "zh-CN") => typeof value === "object" && value !== null ? value[locale] || value["zh-CN"] || value.en || Object.values(value)[0] || "" : value || "";
  const field = (key, label, type = "textarea") => ({ key, label: { "zh-CN": label }, description: {}, type, required: true, collapsed: true });
  const clone = (value) => structuredClone(value);
  const choiceLines = (item, locale) => (item.options || []).map((option) => option.value + " | " + text(option.label, locale)).join("\n");
  const editableChoices = (item, locale) => (item.options || []).every((option) => typeof option.value === "string" && !/[|\r\n]/.test(option.value) && !/[\r\n]/.test(text(option.label, locale)));
  const kinds = ["input", "process", "display", "end"];
  const fieldTypes = ["text", "textarea", "select", "boolean", "file", "image", "images", "email", "url", "number"];
  const presets = {
    simple: () => null,
    aladdin: () => ({ version: 1, entry: "question_1", nodes: [
      ...[1, 2, 3].flatMap((number) => [
        { id: `question_${number}`, kind: "input", label: { "zh-CN": `第 ${number} 个问题`, en: `Question ${number}` }, prompt: { "zh-CN": "准备好了吗？开始后，请在限定时间内提交问题。", en: "Ready? Submit your question within the time limit after starting." }, question: { "zh-CN": `请提出你的第 ${number} 个问题。`, en: `Ask your ${number === 1 ? "first" : number === 2 ? "second" : "third"} question.` }, fields: [field("question", "你的问题")], start_policy: number === 1 ? "confirm" : "automatic", ...(number > 1 ? { show_from: { previous_answer: { node: `answer_${number - 1}`, field: "answer" } } } : {}), timeout_seconds: 300, timeout_next: "timed_out", next: `answer_${number}` },
        { id: `answer_${number}`, kind: "process", label: { "zh-CN": `回答第 ${number} 个问题`, en: `Answer question ${number}` }, inputs: { question: { node: `question_${number}`, field: "question" } }, outputs: [field("answer", "回答")], timeout_seconds: 300, timeout_next: "timed_out", failure_next: "timed_out", next: number < 3 ? `question_${number + 1}` : "finished" },
      ]),
      { id: "finished", kind: "end", state: "succeeded", result: { content: { node: "answer_3", field: "answer" } }, message: { "zh-CN": "三个问题已回答。", en: "Your three questions have been answered." } },
      { id: "timed_out", kind: "end", state: "failed", retryable: true, message: { "zh-CN": "本步骤已超时，请检查需求后重试。", en: "This step timed out. Review your request and retry." } },
    ] }),
    confirmation: () => ({ version: 1, entry: "details", nodes: [
      { id: "details", kind: "input", label: { "zh-CN": "提交需求", en: "Submit requirements" }, prompt: { "zh-CN": "准备好需求后，点击开始。", en: "Start when your requirements are ready." }, question: { "zh-CN": "请填写本次需求。", en: "Describe your requirements." }, fields: [field("requirements", "需求说明")], start_policy: "confirm", next: "process" },
      { id: "process", kind: "process", label: { "zh-CN": "处理需求", en: "Process requirements" }, inputs: { requirements: { node: "details", field: "requirements" } }, outputs: [field("content", "处理结果")], next: "finished", failure_next: "not_completed", timeout_seconds: 3600, timeout_next: "not_completed" },
      { id: "finished", kind: "end", state: "succeeded", result: { content: { node: "process", field: "content" } } },
      { id: "not_completed", kind: "end", state: "failed", retryable: true, message: { "zh-CN": "本次处理未完成，请检查说明后重试。", en: "Processing did not finish. Review the explanation before retrying." } },
    ] }),
  };

  function edges(value) {
    if (!value) return [];
    return value.nodes.flatMap((node) => {
      const result = [];
      if (node.kind !== "end") {
        if (typeof node.next === "string") result.push({ from: node.id, to: node.next, kind: "next" });
        else if (node.next && Array.isArray(node.next.cases)) {
          node.next.cases.forEach((item, index) => result.push({ from: node.id, to: item.to, kind: "case", index, when: item.when }));
          result.push({ from: node.id, to: node.next.default, kind: "default" });
        }
      }
      if (node.failure_next) result.push({ from: node.id, to: node.failure_next, kind: "failure" });
      if (node.timeout_next) result.push({ from: node.id, to: node.timeout_next, kind: node.kind === "process" ? "review" : "timeout" });
      return result;
    });
  }

  function sourceField(value, reference, visited = new Set()) {
    if (!reference || typeof reference !== "object") return null;
    const identity = `${reference.node}.${reference.field}`;
    if (visited.has(identity)) return null;
    visited.add(identity);
    const node = value.nodes.find((item) => item.id === reference.node);
    if (!node) return null;
    if (["input", "process"].includes(node.kind)) return (node.fields || node.outputs || []).find((item) => item.key === reference.field) || null;
    return sourceField(value, (node.show_from || node.result || {})[reference.field], visited);
  }

  function sources(value, purpose = "inputs") {
    if (!value) return [];
    return value.nodes.filter((node) => ["input", "process"].includes(node.kind)).flatMap((node) => {
      const fields = ["input", "process"].includes(node.kind) ? (node.fields || node.outputs || []).map((item) => item.key) : Object.keys(node.show_from || node.result || {});
      return fields.flatMap((key) => {
        const reference = { node: node.id, field: key }, definition = sourceField(value, reference);
        if (!definition || (purpose !== "inputs" && definition.sensitive) || (purpose === "show_from" && ["file", "image", "images"].includes(definition.type))) return [];
        return [{ reference, definition }];
      });
    });
  }

  function diagnostics(value) {
    if (!value) return [];
    const result = [];
    try { validate(value); } catch (error) { result.push({ level: "error", message: error.message }); }
    if (result.length) return result;
    const reachable = new Set(), pending = [value.entry], paths = edges(value);
    while (pending.length) {
      const id = pending.pop();
      if (reachable.has(id)) continue;
      reachable.add(id);
      pending.push(...paths.filter((item) => item.from === id).map((item) => item.to));
    }
    for (const node of value.nodes) if (!reachable.has(node.id)) result.push({ level: "warning", node_id: node.id, code: "unreachable" });
    return result;
  }

  function validate(value) {
    if (value === null) return null;
    if (!value || typeof value !== "object" || Array.isArray(value) || value.version !== 1 || !Array.isArray(value.nodes) || !value.nodes.length || value.nodes.length > 64)
      throw new Error("编排需要 version: 1、开始步骤与 1 至 64 个步骤。");
    const ids = new Set();
    for (const node of value.nodes) {
      if (!node || !/^[a-z][a-z0-9_-]{0,63}$/.test(node.id || "") || ids.has(node.id)) throw new Error("步骤标识须唯一，以小写字母开头，只包含小写字母、数字、下划线或连字符。");
      if (!kinds.includes(node.kind)) throw new Error(`步骤 ${node.id} 的类型无效。`);
      if (node.kind === "end" && !["succeeded", "failed", "rejected"].includes(node.state)) throw new Error(`结束步骤 ${node.id} 的状态无效。`);
      if (node.kind === "end" && node.state !== "succeeded" && Object.keys(node.result || {}).length) throw new Error("失败或拒绝结束步骤不能保留交付映射。");
      const definitions = node.kind === "input" ? node.fields : node.kind === "process" ? node.outputs : [];
      if (!Array.isArray(definitions) || definitions.length > 30) throw new Error(`步骤 ${node.id} 最多支持 30 个字段。`);
      const keys = new Set();
      for (const item of definitions) {
        if (!item || !/^[a-z][a-z0-9_]{0,39}$/.test(item.key || "") || keys.has(item.key)) throw new Error(`步骤 ${node.id} 的字段标识无效或重复。`);
        if (!fieldTypes.includes(item.type || "text")) throw new Error(`步骤 ${node.id} 的字段类型无效。`);
        keys.add(item.key);
      }
      if (node.timeout_seconds !== undefined && node.timeout_seconds !== null && (!Number.isSafeInteger(node.timeout_seconds) || node.timeout_seconds <= 0 || node.timeout_seconds > 86400)) throw new Error(`步骤 ${node.id} 的超时秒数须为 1 至 86400 的整数。`);
      if (node.kind === "process" && (!node.failure_next || !node.timeout_seconds || !node.timeout_next)) throw new Error(`处理步骤 ${node.id} 必须配置失败与超时跳转。`);
      ids.add(node.id);
    }
    if (!ids.has(value.entry) || !["input", "display"].includes(value.nodes.find((node) => node.id === value.entry).kind)) throw new Error("开始步骤必须是存在的顾客填写或展示步骤。");
    const checkReference = (reference, purpose) => {
      if (!reference || !/^[a-z][a-z0-9_-]{0,63}$/.test(reference.node || "") || !/^[a-z][a-z0-9_]{0,39}$/.test(reference.field || "")) throw new Error("引用需要有效的步骤与字段标识。");
      const source = sourceField(value, reference);
      if (!source) throw new Error(`字段引用 ${reference.node}.${reference.field} 不存在或形成循环。`);
      if (source.sensitive && purpose !== "inputs") throw new Error("敏感字段只能传给处理步骤，不能展示、交付或用于条件判断。");
    };
    for (const node of value.nodes) {
      if (node.timeout_seconds && !node.timeout_next) throw new Error(`步骤 ${node.id} 配置超时后，须选择超时跳转步骤。`);
      if (node.kind !== "end" && typeof node.next !== "string") {
        if (node.kind === "display") throw new Error("展示步骤只支持直接跳转。");
        if (!node.next || !Array.isArray(node.next.cases) || !node.next.cases.length || node.next.cases.length > 32) throw new Error(`步骤 ${node.id} 需要 1 至 32 条条件。`);
        for (const item of node.next.cases) {
          const when = item.when;
          if (!when || !["eq", "in", "exists"].includes(when.op)) throw new Error("条件只支持相等、属于列表或存在。");
          checkReference(when.source, "branch");
          if (when.op === "eq" && typeof when.value !== "string") throw new Error("相等条件需要文本值。");
          if (when.op === "in" && (!Array.isArray(when.value) || !when.value.length || when.value.length > 50 || when.value.some((item) => typeof item !== "string"))) throw new Error("属于列表条件需要 1 至 50 个文本值。");
          if (when.op === "exists" && Object.hasOwn(when, "value")) throw new Error("存在条件不需要比较值。");
        }
      }
      for (const key of ["inputs", "show_from", "result"]) if (node[key]) {
        if (typeof node[key] !== "object" || Array.isArray(node[key]) || Object.keys(node[key]).length > 30) throw new Error("字段映射需要对象，最多 30 项。");
        for (const [keyName, reference] of Object.entries(node[key])) {
          if (!/^[a-z][a-z0-9_]{0,39}$/.test(keyName)) throw new Error("映射键以小写字母开头，最多 40 字符。");
          checkReference(reference, key);
        }
      }
    }
    for (const edge of edges(value)) if (!ids.has(edge.to)) throw new Error(`步骤 ${edge.from} 指向不存在的步骤 ${edge.to}。`);
    const reachable = new Set(), pending = [value.entry], paths = edges(value);
    while (pending.length) { const id = pending.pop(); if (!reachable.has(id)) { reachable.add(id); pending.push(...paths.filter((item) => item.from === id).map((item) => item.to)); } }
    if (!value.nodes.some((node) => node.kind === "end" && reachable.has(node.id))) throw new Error("开始步骤必须能到达至少一个结束步骤。");
    const encoded = JSON.stringify(value);
    const size = typeof TextEncoder !== "undefined" ? new TextEncoder().encode(encoded).length : unescape(encodeURIComponent(encoded)).length;
    if (size > 100000) throw new Error("编排定义超过 100,000 UTF-8 字节，请精简字段和说明。");
    return clone(value);
  }

  function graphModel(value) {
    if (!value) return { nodes: [], edges: [], width: 360, height: 180 };
    const paths = edges(value), levels = new Map([[value.entry, 0]]), queue = [value.entry];
    while (queue.length) {
      const id = queue.shift();
      for (const edge of paths.filter((item) => item.from === id && !["failure", "timeout", "review"].includes(item.kind))) if (!levels.has(edge.to) && value.nodes.some((node) => node.id === edge.to)) { levels.set(edge.to, levels.get(id) + 1); queue.push(edge.to); }
    }
    const maximum = Math.max(0, ...levels.values()), primaryCounts = new Map();
    for (const node of value.nodes) if (levels.has(node.id)) primaryCounts.set(levels.get(node.id), (primaryCounts.get(levels.get(node.id)) || 0) + 1);
    const sideColumn = Math.max(1, ...primaryCounts.values()), primary = new Set(levels.keys());
    const columns = new Map(), sides = new Map();
    const nodes = value.nodes.map((node) => {
      let level, column;
      if (primary.has(node.id)) { level = levels.get(node.id); column = columns.get(level) || 0; columns.set(level, column + 1); }
      else { const incoming = paths.filter((edge) => edge.to === node.id && levels.has(edge.from)); level = incoming.length ? Math.min(...incoming.map((edge) => levels.get(edge.from) + 1)) : maximum + 1; column = sideColumn + (sides.get(level) || 0); sides.set(level, column - sideColumn + 1); }
      return { ...node, x: 36 + column * 252, y: 30 + level * 120, width: 224, height: 76 };
    });
    const width = Math.max(360, ...nodes.map((node) => node.x + node.width + 36));
    const height = Math.max(200, ...nodes.map((node) => node.y + node.height + 40));
    return { nodes, edges: paths, width, height };
  }

  function mount(container, options = {}) {
    let definition = clone(options.value || null), selected = definition?.entry || "", changed = false, disposed = false, generation = 0, editRevision = 0, zoomMode = "actual";
    const disabled = options.disabled === true;
    const locale = () => (typeof options.language === "function" ? options.language() : options.lang) === "en" ? "en" : "zh-CN";
    const tr = (cn, en) => locale() === "en" ? en : cn;
    const label = (value) => text(value, locale());
    const kindLabel = (kind) => ({ input: tr("顾客填写", "Customer input"), process: tr("处理任务", "Processing"), display: tr("展示与确认", "Display / confirm"), end: tr("结束", "End") })[kind];
    const active = () => !disposed && (options.active ? options.active() : container.isConnected !== false);
    const pick = (selector) => container.querySelector(selector);
    const notify = (error) => { const node = pick("#task-flow-editor-error"); if (node && active()) node.textContent = error.message || String(error); };
    const disabledAttr = disabled ? "disabled" : "";
    const input = (id, title, value, type = "text", attributes = "") => `<div class="field"><label for="${id}">${escape(title)}</label><input id="${id}" type="${type}" value="${escape(value ?? "")}" ${disabledAttr} ${attributes}></div>`;
    const area = (id, title, value, attributes = "") => `<div class="field"><label for="${id}">${escape(title)}</label><textarea id="${id}" ${disabledAttr} ${attributes}>${escape(value ?? "")}</textarea></div>`;
    const optionList = (value, empty = false, entry = false) => `${empty ? `<option value="">${tr("不跳转", "No transition")}</option>` : ""}${definition.nodes.filter((node) => !entry || ["input", "display"].includes(node.kind)).map((node) => `<option value="${escape(node.id)}" ${node.id === value ? "selected" : ""}>${escape(label(node.label) || node.id)} · ${escape(node.id)}</option>`).join("")}`;
    const select = (id, title, values, current, attributes = "") => `<div class="field"><label for="${id}">${escape(title)}</label><select id="${id}" ${disabledAttr} ${attributes}>${values.map(([value, name]) => `<option value="${escape(value)}" ${value === current ? "selected" : ""}>${escape(name)}</option>`).join("")}</select></div>`;
    const destination = (id, title, value, empty = false, entry = false) => `<div class="field"><label for="${id}">${escape(title)}</label><select id="${id}" ${disabledAttr}>${optionList(value, empty, entry)}</select></div>`;
    const sourceOptions = (purpose, current) => {
      const source = current ? `${current.node}.${current.field}` : "";
      const available = sources(definition, purpose);
      const options = available.map(({ reference, definition: item }) => {
        const node = definition.nodes.find((node) => node.id === reference.node);
        return [`${reference.node}.${reference.field}`, `${label(node.label) || node.id} / ${label(item.label) || reference.field} · ${reference.node}.${reference.field}`];
      });
      if (source && !options.some(([value]) => value === source)) options.unshift([source, tr("无效来源：", "Invalid source: ") + source]);
      return { options: [["", tr("选择来源字段", "Choose a source field")], ...options], source };
    };
    const sourceSelect = (id, title, purpose, current, attributes = "") => { const values = sourceOptions(purpose, current); return select(id, title, values.options, values.source, attributes); };
    const parseReference = (value) => { const split = value.indexOf("."); return { node: value.slice(0, split), field: value.slice(split + 1) }; };
    const capture = () => {
      if (!definition || disabled || !active()) return;
      const node = definition.nodes.find((item) => item.id === selected);
      if (!node || !pick("#tf-label")) return;
      node.label = { ...(node.label || {}), [locale()]: pick("#tf-label").value };
      for (const [id, key] of [["tf-prompt", "prompt"], ["tf-question", "question"], ["tf-display-content", "content"], ["tf-message", "message"]]) if (pick("#" + id)) node[key] = { ...(node[key] || {}), [locale()]: pick("#" + id).value };
      for (const [id, key] of [["tf-start-policy", "start_policy"], ["tf-failure-next", "failure_next"], ["tf-end-state", "state"]]) if (pick("#" + id)) node[key] = pick("#" + id).value;
      if (pick("#tf-retryable")) node.retryable = pick("#tf-retryable").checked;
      if (pick("#tf-needs-review")) node.needs_review = pick("#tf-needs-review").checked;
      if (pick("#tf-next")) node.next = pick("#tf-next").value;
      else if (pick("#tf-branch-default")) node.next = { cases: node.next.cases.map((item, index) => {
        const prefix = "tf-case-" + index, op = pick("#" + prefix + "-op").value;
        const when = { source: parseReference(pick("#" + prefix + "-source").value), op };
        if (op === "eq") when.value = pick("#" + prefix + "-value")?.value || "";
        if (op === "in") when.value = (pick("#" + prefix + "-value")?.value || "").split("\n");
        return { when, to: pick("#" + prefix + "-to").value };
      }), default: pick("#tf-branch-default").value };
      if (pick("#tf-timeout")) {
        const timeout = pick("#tf-timeout").value.trim();
        if (timeout) { node.timeout_seconds = Number(timeout); node.timeout_next = pick("#tf-timeout-next").value; }
        else { delete node.timeout_seconds; delete node.timeout_next; }
      }
      for (const key of ["fields", "outputs"]) if (pick("#tf-" + key + "-list")) node[key] = (node[key] || []).map((item, index) => {
        const prefix = "tf-" + key + "-" + index;
        const updated = { ...item, key: pick("#" + prefix + "-key").value, label: { ...(item.label || {}), [locale()]: pick("#" + prefix + "-label").value }, type: pick("#" + prefix + "-type").value, required: pick("#" + prefix + "-required").checked };
        if (updated.type === "select" && pick("#" + prefix + "-choices")) {
          const choices = pick("#" + prefix + "-choices").value;
          // Rendering is a projection, not a serialization format. Preserve the
          // source when untouched, especially legal values containing "|" and
          // translated labels containing line breaks.
          if (editableChoices(item, locale()) && choices !== choiceLines(item, locale())) updated.options = choices.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
            const split = line.indexOf("|"), value = (split < 0 ? line : line.slice(0, split)).trim(), name = (split < 0 ? line : line.slice(split + 1)).trim();
            const previous = (item.options || []).find((option) => option.value === value);
            return { value, label: { ...(previous?.label || {}), [locale()]: name } };
          });
        } else if (updated.type !== "select") delete updated.options;
        if (updated.type === "images") updated.max_items = Number(pick("#" + prefix + "-max")?.value || item.max_items || 10); else delete updated.max_items;
        if (key === "fields" && updated.type === "text" && pick("#" + prefix + "-sensitive")?.checked) { updated.sensitive = true; updated.sensitive_ttl_seconds = Number(pick("#" + prefix + "-ttl")?.value || 120); }
        else { delete updated.sensitive; delete updated.sensitive_ttl_seconds; }
        return updated;
      });
      for (const key of ["inputs", "show_from", "result"]) {
        if (!pick("#tf-map-" + key)) continue;
        const original = node[key] || (key === "show_from" && node.kind === "display" ? node.result : {}) || {}, value = {};
        Object.keys(original).forEach((originalKey, index) => {
          const prefix = `tf-map-${key}-${index}`, name = pick("#" + prefix + "-key").value;
          if (Object.hasOwn(value, name)) throw new Error(tr("映射键重复，请为每行填写不同名称。", "Mapping keys must be unique."));
          value[name] = parseReference(pick("#" + prefix + "-source").value);
        });
        node[key] = value;
        if (key === "show_from" && node.kind === "display") delete node.result;
      }
      definition.entry = pick("#tf-entry")?.value || definition.entry;
    };
    const mapping = (key, title, value, hint) => `<section id="tf-map-${key}" class="task-flow-map"><div class="section-head"><h5>${escape(title)}</h5><button type="button" class="secondary" data-tf-add-map="${key}" ${disabledAttr}>${tr("添加映射", "Add mapping")}</button></div><p class="caption">${hint}</p>${Object.entries(value || {}).map(([name, reference], index) => `<div class="task-flow-map-row">${input(`tf-map-${key}-${index}-key`, tr("目标字段", "Destination field"), name, "text", 'maxlength="40"')}${sourceSelect(`tf-map-${key}-${index}-source`, tr("来源", "Source"), key, reference)}<button type="button" class="task-flow-delete secondary" data-tf-remove-map="${key}:${index}" aria-label="${tr("删除映射", "Remove mapping")} ${escape(name)}" ${disabledAttr}>${tr("删除", "Remove")}</button></div>`).join("") || `<p class="task-flow-empty">${tr("还没有映射。添加后，只有这些字段会被使用。", "No mappings yet. Only fields you select here will be used.")}</p>`}</section>`;
    const schemaFields = (key, definitions, title) => `<section id="tf-${key}-list" class="task-flow-schema-fields"><div class="section-head"><h5>${escape(title)}</h5><button type="button" class="secondary" data-tf-add-field="${key}" ${disabledAttr}>${tr("添加字段", "Add field")}</button></div>${definitions.map((item, index) => {
      const prefix = "tf-" + key + "-" + index;
      const names = [tr("文本", "Text"), tr("多行文本", "Long text"), tr("选择", "Choice"), tr("是 / 否", "Yes / no"), tr("文件", "File"), tr("图片", "Image"), tr("图片集", "Image collection"), tr("邮箱", "Email"), tr("网址", "URL"), tr("数字", "Number")];
      return `<div class="task-flow-field-row"><div class="grid">${input(prefix + "-label", tr("显示名称", "Display name"), label(item.label))}${input(prefix + "-key", tr("字段标识", "Field key"), item.key, "text", 'maxlength="40"')}</div><div class="grid">${select(prefix + "-type", tr("字段类型", "Field type"), fieldTypes.map((value, i) => [value, names[i]]), item.type, `data-tf-field-type="${key}"`)}${item.type === "images" ? input(prefix + "-max", tr("最多图片数", "Maximum images"), item.max_items || 10, "number", 'min="1" max="20"') : ""}</div>${item.type === "select" ? `${area(prefix + "-choices", tr("选项（每行：固定值 | 显示名称）", "Choices (one value | label per line)"), choiceLines(item, locale()), editableChoices(item, locale()) ? "" : "readonly")}${!editableChoices(item, locale()) ? `<p class="caption">${tr("选项含分隔符或多行名称，请在下方字段 JSON 中编辑。此处保留原始选项。", "These choices contain separators or multiline labels. Edit them in field JSON below; the original choices are preserved here.")}</p>` : ""}` : ""}${key === "fields" && item.type === "text" ? `<div class="task-flow-sensitive"><label><input id="${prefix}-sensitive" type="checkbox" ${item.sensitive ? "checked" : ""} ${disabledAttr} data-tf-sensitive="${key}">${tr("敏感内容，如验证码", "Sensitive value, such as a verification code")}</label>${item.sensitive ? input(prefix + "-ttl", tr("保存有效期（秒）", "Retention lifetime (seconds)"), item.sensitive_ttl_seconds || 120, "number", 'min="1" max="600"') : ""}</div>` : ""}<div class="section-head"><label><input id="${prefix}-required" type="checkbox" ${item.required !== false ? "checked" : ""} ${disabledAttr}>${tr("必填", "Required")}</label><button type="button" class="task-flow-delete secondary" data-tf-remove-field="${key}:${index}" ${disabledAttr}>${tr("删除字段", "Remove field")}</button></div></div>`;
    }).join("")}<details class="task-flow-field-advanced"><summary>${tr("教程、多语言与字段 JSON", "Tutorials, translations and field JSON")}</summary>${area("tf-" + key + "-json", tr("字段 JSON", "Field JSON"), JSON.stringify(definitions, null, 2), 'spellcheck="false" class="mono"')}<button type="button" class="secondary" data-tf-apply-fields="${key}" ${disabledAttr}>${tr("应用字段 JSON", "Apply field JSON")}</button></details></section>`;
    const routeEditor = (node) => {
      if (node.kind === "end") return "";
      const branched = typeof node.next === "object";
      return `<section class="task-flow-routing"><h5>${tr("完成后的路径", "After this step")}</h5>${node.kind !== "display" ? select("tf-route-mode", tr("跳转方式", "Routing"), [["direct", tr("直接进入下一步", "Go to the next step")], ["branch", tr("按条件选择路径", "Choose a path by condition")]], branched ? "branch" : "direct") : ""}${branched ? `<p class="caption">${tr("从上到下匹配，命中第一条即跳转。", "Conditions run from top to bottom; the first match wins.")}</p>${node.next.cases.map((item, index) => {
        const prefix = "tf-case-" + index, when = item.when;
        return `<div class="task-flow-condition"><div class="section-head"><strong>${tr("条件", "Condition")} ${index + 1}</strong><div class="task-flow-condition-actions"><button type="button" class="secondary" data-tf-move-case="${index}" ${disabled || index === 0 ? "disabled" : ""}>${tr("上移", "Move up")}</button><button type="button" class="task-flow-delete secondary" data-tf-remove-case="${index}" ${disabledAttr}>${tr("删除", "Remove")}</button></div></div>${sourceSelect(prefix + "-source", tr("判断字段", "Compare field"), "branch", when.source)}${select(prefix + "-op", tr("条件", "Operation"), [["eq", tr("等于", "Equals")], ["in", tr("属于以下任一值", "Is one of")], ["exists", tr("已填写", "Has a value")]], when.op, 'data-tf-case-op="true"')}${when.op === "in" ? area(prefix + "-value", tr("比较值（每行一个）", "Values (one per line)"), (when.value || []).join("\n")) : when.op === "eq" ? input(prefix + "-value", tr("比较值", "Value"), when.value ?? "") : ""}${destination(prefix + "-to", tr("满足时进入", "When matched, go to"), item.to)}</div>`;
      }).join("")}<button type="button" id="tf-add-case" class="secondary" ${disabled || node.next.cases.length >= 32 ? "disabled" : ""}>${tr("添加条件", "Add condition")}</button>${destination("tf-branch-default", tr("都不满足时进入", "Otherwise, go to"), node.next.default)}` : destination("tf-next", tr("下一步", "Next step"), node.next)}<div class="task-flow-exceptions"><h5>${tr("超时与异常", "Timeouts and failures")}</h5>${node.kind === "process" ? destination("tf-failure-next", tr("明确处理失败时进入", "When processing fails, go to"), node.failure_next) : ""}<div class="grid">${input("tf-timeout", tr(node.kind === "process" ? "处理期限（秒）" : "填写 / 确认期限（秒，可留空）", node.kind === "process" ? "Processing deadline (seconds)" : "Response deadline (seconds, optional)"), node.timeout_seconds, "number", 'min="1" max="86400" step="1"')}${destination("tf-timeout-next", tr(node.kind === "process" ? "超时记录的目标" : "超时后进入", node.kind === "process" ? "Recorded timeout target" : "On timeout, go to"), node.timeout_next, true)}</div><p class="caption">${node.kind === "process" ? tr("处理超时会暂停并要求商家核实，不会自动执行目标步骤。进度更新不会延长期限。", "A processing timeout stops for merchant review; it never executes the target automatically. Progress updates do not extend the deadline.") : tr("计时由服务器执行。超时按这里的路径跳转；首步始终等待顾客点击开始。", "The server controls the timer and timeout route. The first step always waits for the customer to start.")}</p></div></section>`;
    };
    const edgeLabel = (edge) => ({ next: tr("完成", "Done"), case: `${tr("条件", "Condition")} ${edge.index + 1}`, default: tr("否则", "Otherwise"), failure: tr("失败", "Failure"), timeout: tr("超时", "Timeout"), review: tr("超时 · 核实", "Timeout · review") })[edge.kind];
    const diagram = () => {
      const model = graphModel(definition), byId = new Map(model.nodes.map((node) => [node.id, node]));
      const paths = model.edges.map((edge, index) => {
        const from = byId.get(edge.from), to = byId.get(edge.to);
        if (!from || !to) return "";
        const x1 = from.x + from.width / 2, y1 = from.y + from.height, x2 = to.x + to.width / 2, y2 = to.y;
        let path, x, y;
        const kind = ["failure", "timeout", "review"].includes(edge.kind) ? "exception" : "normal", current = edge.from === selected || edge.to === selected;
        if (kind === "exception" && to.x > from.x) { const start = from.x + from.width, lane = (start + to.x) / 2; path = `M${start},${from.y + 38} C${lane},${from.y + 38} ${lane},${to.y + 38} ${to.x},${to.y + 38}`; x = lane; y = from.y + 20; }
        else if (y2 > y1) { const middle = (y1 + y2) / 2; path = `M${x1},${y1} C${x1},${middle} ${x2},${middle} ${x2},${y2}`; x = (x1 + x2) / 2; y = middle; }
        else { const outside = Math.max(10, Math.min(from.x, to.x) - 15 - index % 4 * 4); path = `M${from.x},${from.y + 38} C${outside},${from.y + 38} ${outside},${to.y + 38} ${to.x},${to.y + 38}`; x = outside + 28; y = (from.y + to.y) / 2 + 38; }
        return `<g class="task-flow-edge ${kind}${current ? " current" : ""}"><path d="${path}" marker-end="url(#tf-arrow-${generation}-${kind})"></path><rect x="${x - 49}" y="${y - 9}" width="98" height="18" rx="4"></rect><text x="${x}" y="${y + 4}" text-anchor="middle">${edgeLabel(edge)}</text></g>`;
      }).join("");
      const previousCanvas = pick(".task-flow-canvas"), scale = zoomMode === "fit" ? Math.min(1, ((previousCanvas?.clientWidth || 360) - 24) / model.width, ((previousCanvas?.clientHeight || 440) - 24) / model.height) : 1;
      return `<div class="task-flow-canvas-controls"><button type="button" id="tf-fit" class="secondary" aria-pressed="${zoomMode === "fit"}">${tr("适应画布", "Fit canvas")}</button><button type="button" id="tf-actual-size" class="secondary" aria-pressed="${zoomMode === "actual"}">100%</button><span>${zoomMode === "fit" ? tr("缩小查看全部路径", "Overview of every route") : tr("滚动浏览，选择步骤编辑", "Scroll to explore; select a step to edit")}</span></div><div class="task-flow-canvas" role="region" aria-label="${tr("流程图，选择步骤编辑", "Flow diagram; select a step to edit")}" tabindex="0"><div class="task-flow-canvas-stage" style="width:${Math.ceil(model.width * scale)}px;height:${Math.ceil(model.height * scale)}px"><div class="task-flow-canvas-inner" style="width:${model.width}px;height:${model.height}px;transform:scale(${scale});transform-origin:top left"><svg width="${model.width}" height="${model.height}" aria-hidden="true" focusable="false"><defs>${["normal", "exception"].map((kind) => `<marker id="tf-arrow-${generation}-${kind}" class="${kind}" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z"></path></marker>`).join("")}</defs>${paths}</svg>${model.nodes.map((node) => `<button type="button" class="task-flow-node ${node.id === selected ? "selected" : "secondary"} ${node.kind}" id="tf-node-${escape(node.id)}" data-tf-node="${escape(node.id)}" style="left:${node.x}px;top:${node.y}px;width:${node.width}px;height:${node.height}px" aria-pressed="${node.id === selected}" aria-label="${escape(label(node.label) || node.id)} · ${kindLabel(node.kind)}${node.id === definition.entry ? " · " + tr("开始步骤", "Entry step") : ""}"><small>${kindLabel(node.kind)}${node.id === definition.entry ? `<span class="task-flow-entry-tag">${tr("开始", "Start")}</span>` : ""}</small><span>${escape(label(node.label) || node.id)}</span></button>`).join("")}</div></div></div>`;
    };
    const inspection = () => {
      const issues = diagnostics(definition);
      return `<details class="task-flow-checks" ${issues.length ? "open" : ""}><summary>${issues.length ? tr(`结构检查 · ${issues.length} 项提醒`, `Structure check · ${issues.length} items`) : tr("结构检查通过", "Structure check passed")}</summary><p class="caption">${tr("这里只检查流程结构。保存商品时，服务器会再校验字段类型、处理器兼容性与交付结构。", "This checks graph structure only. Saving validates field types, processor compatibility and delivery schema on the server.")}</p>${issues.length ? `<ul>${issues.map((issue) => `<li class="${issue.level === "error" ? "error" : ""}">${escape(issue.message || tr(`步骤 ${issue.node_id} 从入口不可达`, `Step ${issue.node_id} cannot be reached from the entry`))}</li>`).join("")}</ul>` : ""}</details>`;
    };
    const draw = (focusId = "") => {
      if (!active()) return;
      const previousCanvas = pick(".task-flow-canvas"), scrollTop = previousCanvas?.scrollTop || 0, scrollLeft = previousCanvas?.scrollLeft || 0;
      generation++;
      const paint = generation;
      const node = definition?.nodes.find((item) => item.id === selected) || definition?.nodes[0];
      if (node) selected = node.id;
      container.innerHTML = `<div class="task-flow-editor"><div class="section-head"><h3>${tr("处理流程", "Task flow")}</h3><label class="task-flow-enable"><input id="tf-enabled" type="checkbox" ${definition ? "checked" : ""} ${disabledAttr}>${tr("启用多步交互", "Enable multiple steps")}</label></div><p class="task-flow-editor-intro caption">${tr("简单商品用一次提交、一次交付。分轮提问、接收验证码或等待顾客确认时，再编排步骤。修改只用于之后发行的卡密。", "Use one submission and delivery for simple products. Add a flow for multiple questions, verification codes or customer confirmation. Changes apply only to newly issued codes.")}</p><div class="task-flow-toolbar"><div class="task-flow-presets"><label for="tf-preset">${tr("从模板开始", "Start from a template")}</label><select id="tf-preset" ${disabledAttr}><option value="">${tr("选择模板", "Choose a template")}</option><option value="aladdin">${tr("阿拉丁神灯 · 三轮问答", "Aladdin · three questions")}</option><option value="confirmation">${tr("收集需求 → 处理 → 交付", "Requirements → processing → delivery")}</option></select><button id="tf-use-preset" type="button" class="secondary" ${disabledAttr}>${tr("使用模板", "Use template")}</button></div>${definition ? `<button id="tf-check" type="button" class="secondary" ${disabledAttr}>${options.validate ? tr("验证配置", "Validate configuration") : tr("检查结构", "Check structure")}</button>` : ""}</div>${definition ? `<div class="task-flow-editor-layout"><section class="task-flow-graph"><div class="task-flow-graph-head">${destination("tf-entry", tr("开始步骤", "Entry step"), definition.entry, false, true)}<div class="task-flow-legend"><span>${tr("实线 · 正常路径", "Solid · normal route")}</span><span>${tr("虚线 · 失败 / 超时", "Dashed · failure / timeout")}</span></div></div>${diagram()}<div class="task-flow-add">${select("tf-new-kind", tr("新步骤类型", "New step type"), kinds.map((kind) => [kind, kindLabel(kind)]), "input")}<button type="button" id="tf-add" class="secondary" ${disabledAttr}>${tr("添加步骤", "Add step")}</button></div>${inspection()}</section><section class="task-flow-node-editor" aria-label="${tr("步骤配置", "Step settings")}"><div class="section-head"><div><h4>${escape(label(node.label) || node.id)}</h4><p class="caption task-flow-node-id">${escape(node.id)} · ${kindLabel(node.kind)}</p></div><button type="button" id="tf-remove" class="task-flow-delete secondary" ${disabled || definition.nodes.length < 2 ? "disabled" : ""}>${tr("删除步骤", "Remove step")}</button></div>${input("tf-label", tr("步骤名称", "Step name"), label(node.label), "text", 'maxlength="200"')}${node.kind === "input" ? `${area("tf-prompt", tr("开始前的提示", "Before starting"), label(node.prompt))}${area("tf-question", tr("填写时的提示", "Input instructions"), label(node.question))}${select("tf-start-policy", tr("开始计时的时机", "Start the timer"), [["confirm", tr("顾客点击开始后", "When the customer starts")], ["automatic", tr("进入这一步时（首步仍需点击）", "On entry (first step still needs Start)")]], node.start_policy || "confirm")}${schemaFields("fields", node.fields || [], tr("顾客填写项", "Customer fields"))}${mapping("show_from", tr("让顾客看到的先前结果", "Previous results shown to the customer"), node.show_from || {}, tr("只展示这里选中的非敏感文本、选择或判断结果，不会自动填入表单。", "Show only selected nonsensitive text, choice or yes/no results. These do not prefill the form."))}` : ""}${node.kind === "process" ? `${mapping("inputs", tr("传给处理者的信息", "Inputs sent to the worker"), node.inputs || {}, tr("目标字段是处理者收到的参数名。来源必须是在执行时已完成的步骤字段。", "Destination fields are the parameter names the worker receives. Choose step fields that will be completed before this step."))}${schemaFields("outputs", node.outputs || [], tr("本步返回的结果", "This step's outputs"))}` : ""}${node.kind === "display" ? `${area("tf-display-content", tr("展示说明", "Display instructions"), label(node.content))}${mapping("show_from", tr("展示的结果", "Results to display"), node.show_from || node.result || {}, tr("顾客查看后点击继续。附件留到最终领取页面。", "The customer reviews these results and continues. Attachments remain on the final receipt."))}` : ""}${node.kind === "end" ? `${select("tf-end-state", tr("结束状态", "Outcome"), [["succeeded", tr("兑换完成", "Succeeded")], ["failed", tr("未完成", "Failed")], ["rejected", tr("拒绝处理", "Rejected")]], node.state)}${area("tf-message", tr("结束说明", "Outcome message"), label(node.message))}<div class="task-flow-outcome-options"><label><input id="tf-retryable" type="checkbox" ${node.retryable ? "checked" : ""} ${disabledAttr}>${tr("失败后允许重试", "Allow retry after failure")}</label><label><input id="tf-needs-review" type="checkbox" ${node.needs_review ? "checked" : ""} ${disabledAttr}>${tr("需要商家核实", "Merchant review required")}</label></div>${node.state === "succeeded" ? mapping("result", tr("最终交付字段的来源", "Sources for final delivery"), node.result || {}, tr("目标字段须对应商品最终输出。附件只能引用处理步骤返回的文件。", "Destination fields must match the product's final outputs. Attachments must come from process outputs.")) : ""}` : routeEditor(node)}</section></div><details id="tf-advanced" class="task-flow-advanced"><summary>${tr("高级 · 导入 / 导出 JSON", "Advanced · import / export JSON")}</summary><p class="caption">${tr("定义版本 v1。条件比较不执行代码；商品处理方式与运行环境在发货对接里配置。", "Definition version v1. Conditions never execute code. Configure the executor and runtime in fulfillment settings.")}</p>${area("tf-definition", tr("完整流程定义", "Full flow definition"), JSON.stringify(definition, null, 2), 'class="mono" spellcheck="false"')}<div class="actions"><button id="tf-apply-json" type="button" class="secondary" ${disabledAttr}>${tr("应用 JSON 定义", "Apply JSON")}</button><button id="tf-export-json" type="button" class="secondary">${tr("导出 JSON", "Export JSON")}</button></div></details>` : `<div class="task-flow-simple"><p>${tr("一次提交需求，一次交付结果。", "One submission, one delivery.")}</p><p class="caption">${tr("无需定义流程图，继续配置顾客填写项与交付结果即可。", "No graph is needed. Continue with the customer fields and delivery outputs.")}</p></div>`}<div id="tf-validation-status" class="task-flow-validation-status" role="status" aria-live="polite"></div><div id="task-flow-editor-error" class="error" role="alert"></div></div>`;
      options.onChange?.(definition !== null);
      const valid = () => active() && paint === generation;
      const on = (selector, event, callback, readonly = false) => pick(selector)?.addEventListener(event, (eventObject) => {
        if (!valid() || (disabled && !readonly)) return;
        try { const result = callback(eventObject); if (!readonly) changed = true; return result; } catch (error) { notify(error); }
      });
      const bindAll = (selector, event, callback, readonly = false) => { for (const control of container.querySelectorAll(selector)) control.addEventListener(event, (eventObject) => { if (!valid() || (disabled && !readonly)) return; try { callback(control, eventObject); if (!readonly) changed = true; } catch (error) { notify(error); } }); };
      const mark = () => { if (valid() && !disabled) { changed = true; editRevision++; const status = pick("#tf-validation-status"); if (status) status.textContent = ""; } };
      for (const control of container.querySelectorAll("input,textarea,select")) control.addEventListener("input", mark);
      for (const control of container.querySelectorAll("input,textarea,select")) control.addEventListener("change", mark);
      on("#tf-enabled", "change", () => { if (!pick("#tf-enabled").checked) { capture(); definition = null; } else definition = presets.confirmation(); selected = definition?.entry || ""; draw("tf-enabled"); });
      on("#tf-use-preset", "click", () => { const preset = pick("#tf-preset").value; if (!preset) throw new Error(tr("先选择一个流程模板。", "Choose a flow template first.")); if (definition && options.confirm && !options.confirm(tr("使用模板将替换当前流程。继续？", "Replace the current flow with this template?"))) return; const replacement = presets[preset](); options.onPreset?.(preset, clone(replacement)); definition = replacement; selected = definition.entry; draw("tf-entry"); });
      const choose = (id, keyboard = false) => { capture(); selected = id; draw(keyboard || disabled ? "tf-node-" + id : "tf-label"); };
      on("#tf-fit", "click", () => { capture(); zoomMode = "fit"; draw("tf-fit"); }, true);
      on("#tf-actual-size", "click", () => { capture(); zoomMode = "actual"; draw("tf-actual-size"); }, true);
      bindAll("[data-tf-node]", "click", (button) => choose(button.dataset.tfNode), true);
      bindAll("[data-tf-node]", "keydown", (button, event) => { if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault(); const index = definition.nodes.findIndex((item) => item.id === button.dataset.tfNode); const target = event.key === "Home" ? 0 : event.key === "End" ? definition.nodes.length - 1 : Math.max(0, Math.min(definition.nodes.length - 1, index + (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1))); choose(definition.nodes[target].id, true); }, true);
      on("#tf-entry", "change", () => { capture(); draw("tf-entry"); });
      on("#tf-check", "click", async () => {
        if (disabled) return;
        const button = pick("#tf-check");
        let submittedRevision, submittedDefinition, submittedProduct;
        try {
          capture();
          const value = validate(definition), product = options.product ? clone(options.product()) : {};
          submittedRevision = editRevision; submittedDefinition = JSON.stringify(definition); submittedProduct = JSON.stringify(product);
          if (!options.validate) { draw("tf-check"); return; }
          button.disabled = true; button.setAttribute?.("aria-busy", "true");
          const status = pick("#tf-validation-status");
          if (status) status.textContent = tr("正在校验流程与商品结构…", "Validating the flow and product schema…");
          await options.validate(clone(value), product);
          if (!valid() || editRevision !== submittedRevision || JSON.stringify(definition) !== submittedDefinition) return;
          if (JSON.stringify(options.product ? options.product() : {}) !== submittedProduct) { if (status) status.textContent = ""; return; }
          if (status) status.textContent = tr("服务器校验通过。保存商品后用于新卡密。", "Server validation passed. Save the product to apply this to new codes.");
        } catch (error) {
          if (valid() && (submittedRevision === undefined || editRevision === submittedRevision)) {
            const status = pick("#tf-validation-status"); if (status) status.textContent = "";
            let sameProduct = true, currentError = error;
            if (submittedProduct !== undefined) {
              try { sameProduct = JSON.stringify(options.product ? options.product() : {}) === submittedProduct; }
              catch (captureError) { currentError = captureError; }
            }
            if (sameProduct) notify(currentError);
          }
        } finally {
          if (valid() && pick("#tf-check") === button) { button.disabled = disabled; button.removeAttribute?.("aria-busy"); }
        }
      }, true);
      bindAll("[data-tf-add-field]", "click", (button) => { capture(); const key = button.dataset.tfAddField, fields = node[key] || (node[key] = []); if (fields.length >= 30) throw new Error(tr("每步最多 30 个字段。", "A step supports up to 30 fields.")); let index = fields.length + 1; while (fields.some((item) => item.key === "field_" + index)) index++; fields.push(field("field_" + index, tr("新字段", "New field"), "text")); draw(`tf-${key}-${fields.length - 1}-label`); });
      bindAll("[data-tf-remove-field]", "click", (button) => { capture(); const [key, index] = button.dataset.tfRemoveField.split(":"), item = node[key][Number(index)]; const references = definition.nodes.flatMap((item) => [...Object.values(item.inputs || {}), ...Object.values(item.show_from || {}), ...Object.values(item.result || {}), ...(typeof item.next === "object" ? item.next.cases.map((item) => item.when.source) : [])]); if (references.some((reference) => reference.node === node.id && reference.field === item.key)) throw new Error(tr("先移除使用此字段的映射或条件，再删除字段。", "Remove mappings or conditions that use this field first.")); node[key].splice(Number(index), 1); draw("tf-label"); });
      bindAll("[data-tf-field-type]", "change", () => { capture(); draw("tf-label"); });
      bindAll("[data-tf-sensitive]", "change", () => { capture(); draw("tf-label"); });
      bindAll("[data-tf-apply-fields]", "click", (button) => { const key = button.dataset.tfApplyFields; const value = JSON.parse(pick("#tf-" + key + "-json").value); if (!Array.isArray(value)) throw new Error(tr("字段定义需要 JSON 数组。", "Field definitions must be a JSON array.")); capture(); node[key] = value; draw("tf-label"); });
      bindAll("[data-tf-add-map]", "click", (button) => { capture(); const key = button.dataset.tfAddMap; const values = node[key] || (node[key] = {}); if (Object.keys(values).length >= 30) throw new Error(tr("最多 30 个字段映射。", "Up to 30 field mappings are supported.")); let index = Object.keys(values).length + 1; while (Object.hasOwn(values, "field_" + index)) index++; values["field_" + index] = sources(definition, key)[0]?.reference || { node: "", field: "" }; draw(`tf-map-${key}-${Object.keys(values).length - 1}-key`); });
      bindAll("[data-tf-remove-map]", "click", (button) => { capture(); const [key, index] = button.dataset.tfRemoveMap.split(":"), name = Object.keys(node[key])[Number(index)]; delete node[key][name]; draw("tf-label"); });
      on("#tf-route-mode", "change", () => { const mode = pick("#tf-route-mode").value; capture(); if (mode === "branch" && typeof node.next === "string") { const to = node.next; node.next = { cases: [{ when: { source: sources(definition, "branch")[0]?.reference || { node: "", field: "" }, op: "exists" }, to }], default: to }; } else if (mode === "direct" && typeof node.next === "object") node.next = node.next.default; draw("tf-route-mode"); });
      on("#tf-add-case", "click", () => { capture(); if (node.next.cases.length >= 32) return; node.next.cases.push({ when: { source: sources(definition, "branch")[0]?.reference || { node: "", field: "" }, op: "exists" }, to: node.next.default }); draw(`tf-case-${node.next.cases.length - 1}-source`); });
      bindAll("[data-tf-remove-case]", "click", (button) => { capture(); if (node.next.cases.length === 1) node.next = node.next.default; else node.next.cases.splice(Number(button.dataset.tfRemoveCase), 1); draw("tf-route-mode"); });
      bindAll("[data-tf-move-case]", "click", (button) => { capture(); const index = Number(button.dataset.tfMoveCase); if (index > 0) [node.next.cases[index - 1], node.next.cases[index]] = [node.next.cases[index], node.next.cases[index - 1]]; draw(`tf-case-${Math.max(0, index - 1)}-source`); });
      bindAll("[data-tf-case-op]", "change", () => { capture(); draw("tf-route-mode"); });
      on("#tf-end-state", "change", () => { capture(); if (node.state !== "succeeded") node.result = {}; draw("tf-end-state"); });
      on("#tf-add", "click", () => {
        capture(); if (definition.nodes.length >= 64) throw new Error(tr("最多配置 64 个步骤。", "Up to 64 steps are supported."));
        const kind = pick("#tf-new-kind").value;
        let count = definition.nodes.length + 1, id = `step_${count}`;
        while (definition.nodes.some((item) => item.id === id)) id = `step_${++count}`;
        let failure = definition.nodes.find((item) => item.kind === "end" && item.state === "failed");
        if (kind === "process" && !failure) {
          if (definition.nodes.length > 62) throw new Error(tr("添加处理步骤还需要失败终点，最多配置 64 个步骤。", "A process also needs a failure endpoint; the limit is 64 steps."));
          let failureId = "failed"; while (definition.nodes.some((item) => item.id === failureId)) failureId += "_";
          failure = { id: failureId, kind: "end", state: "failed", retryable: true, message: { [locale()]: tr("本次处理未完成。", "Processing did not finish.") } }; definition.nodes.push(failure);
        }
        const newNode = { id, kind, label: { [locale()]: tr("新步骤", "New step") }, ...(kind === "end" ? { state: "succeeded", result: {} } : { next: definition.nodes.find((item) => item.kind === "end")?.id || definition.entry }), ...(kind === "input" ? { fields: [field("text", tr("请填写", "Your input"))], start_policy: "confirm", prompt: {}, question: {} } : {}), ...(kind === "process" ? { inputs: {}, outputs: [field("content", tr("处理结果", "Result"))], failure_next: failure.id, timeout_seconds: 3600, timeout_next: failure.id } : {}) };
        definition.nodes.push(newNode); selected = id; draw("tf-label");
      });
      on("#tf-remove", "click", () => { capture(); if (definition.nodes.length < 2) return; const references = definition.nodes.filter((item) => item.id !== selected).flatMap((item) => [...Object.values(item.inputs || {}), ...Object.values(item.show_from || {}), ...Object.values(item.result || {}), ...(typeof item.next === "object" ? item.next.cases.map((item) => item.when.source) : [])]); if (definition.entry === selected || edges(definition).some((item) => item.from !== selected && item.to === selected) || references.some((reference) => reference.node === selected)) throw new Error(tr("先修改入口、指向此步骤的路径与字段引用，再删除。", "Update the entry, incoming routes and field references before removing this step.")); definition.nodes = definition.nodes.filter((item) => item.id !== selected); selected = definition.entry; draw("tf-label"); });
      on("#tf-apply-json", "click", () => { let value; try { value = JSON.parse(pick("#tf-definition").value); } catch { throw new Error(tr("完整流程定义的 JSON 格式有误。", "The flow definition is not valid JSON.")); } definition = validate(value); selected = definition?.entry || ""; draw("tf-label"); });
      on("#tf-advanced", "toggle", () => { if (pick("#tf-advanced").open) { capture(); pick("#tf-definition").value = JSON.stringify(definition, null, 2); } });
      on("#tf-export-json", "click", () => { capture(); const value = validate(definition); if (options.export) { options.export(clone(value)); return; } if (typeof Blob === "undefined" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") throw new Error(tr("此浏览器不支持下载，请复制上方 JSON。", "Downloads are unavailable; copy the JSON above.")); const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + "\n"], { type: "application/json;charset=utf-8" })); try { const link = document.createElement("a"); link.href = url; link.download = "extore-task-flow-v1.json"; link.click(); } finally { URL.revokeObjectURL(url); } }, true);
      const canvas = pick(".task-flow-canvas"); if (canvas) { canvas.scrollTop = scrollTop; canvas.scrollLeft = scrollLeft; }
      if (focusId) pick("#" + focusId)?.focus?.({ preventScroll: true });
    };
    draw();
    return { getValue() { if (!active()) throw new Error(tr("流程编辑页面已失效，请重新打开。", "This editor is no longer active. Reopen it.")); capture(); return validate(definition); }, hasChanges() { return changed; }, dispose() { disposed = true; generation++; definition = null; } };
  }
  window.ExtoreTaskFlowEditor = { mount, presets, validate, diagnostics, graphModel, sources };
})();
