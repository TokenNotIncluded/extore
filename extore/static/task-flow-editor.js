"use strict";

(() => {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const text = (value) => typeof value === "object" && value !== null ? value["zh-CN"] || value.en || Object.values(value)[0] || "" : value || "";
  const field = (key, label, type = "textarea") => ({ key, label: { "zh-CN": label }, description: {}, type, required: true, collapsed: true });
  const clone = (value) => structuredClone(value);
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
      { id: "details", kind: "input", label: { "zh-CN": "提交需求" }, prompt: { "zh-CN": "准备好需求后，点击开始。" }, question: { "zh-CN": "请填写本次需求。" }, fields: [field("requirements", "需求说明")], start_policy: "confirm", next: "process" },
      { id: "process", kind: "process", label: { "zh-CN": "处理需求" }, inputs: { requirements: { node: "details", field: "requirements" } }, outputs: [field("content", "处理结果")], next: "finished", failure_next: "not_completed", timeout_seconds: 3600, timeout_next: "not_completed" },
      { id: "finished", kind: "end", state: "succeeded", result: { content: { node: "process", field: "content" } } },
      { id: "not_completed", kind: "end", state: "failed", retryable: true, message: { "zh-CN": "本次处理未完成，请检查说明后重试。" } },
    ] }),
  };

  function validate(value) {
    if (value === null) return null;
    if (!value || typeof value !== "object" || Array.isArray(value) || value.version !== 1 || !Array.isArray(value.nodes) || !value.nodes.length || value.nodes.length > 64)
      throw new Error("编排需要 version: 1、开始步骤与 1 至 64 个步骤。");
    const ids = new Set();
    for (const node of value.nodes) {
      if (!node || !/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(node.id || "") || ids.has(node.id)) throw new Error("步骤标识须唯一，以字母开头，只包含字母、数字、下划线或连字符。");
      if (!["input", "process", "display", "end"].includes(node.kind)) throw new Error(`步骤 ${node.id} 的类型无效。`);
      if (node.timeout_seconds !== undefined && node.timeout_seconds !== null && (!Number.isSafeInteger(node.timeout_seconds) || node.timeout_seconds <= 0 || node.timeout_seconds > 86400)) throw new Error(`步骤 ${node.id} 的超时秒数须为 1 至 86400 的整数。`);
      if (node.kind === "process" && (!node.failure_next || !node.timeout_seconds || !node.timeout_next)) throw new Error(`处理步骤 ${node.id} 必须配置失败与超时跳转。`);
      ids.add(node.id);
    }
    if (!ids.has(value.entry)) throw new Error("请选择存在的开始步骤。");
    for (const node of value.nodes) {
      for (const target of [node.timeout_next, node.failure_next, typeof node.next === "string" ? node.next : null]) if (target && !ids.has(target)) throw new Error(`步骤 ${node.id} 指向不存在的步骤 ${target}。`);
      if (node.timeout_seconds && !node.timeout_next) throw new Error(`步骤 ${node.id} 配置超时后，须选择超时跳转步骤。`);
    }
    if (JSON.stringify(value).length > 100000) throw new Error("编排定义过大，请精简字段和说明。");
    return clone(value);
  }

  function mount(container, options = {}) {
    let definition = clone(options.value || null);
    let selected = definition?.entry || "";
    let changed = false;
    const disabled = options.disabled === true;
    const notify = (error) => { const node = container.querySelector("#task-flow-editor-error"); if (node) node.textContent = error.message || String(error); };
    const active = () => options.active ? options.active() : container.isConnected !== false;
    const pick = (selector) => container.querySelector(selector);
    const capture = () => {
      if (!definition) return;
      const node = definition.nodes.find((item) => item.id === selected);
      if (!node || !pick("#tf-label")) return;
      node.label = { ...(node.label || {}), "zh-CN": pick("#tf-label").value };
      if (pick("#tf-next")) node.next = pick("#tf-next").value;
      if (pick("#tf-start-policy")) node.start_policy = pick("#tf-start-policy").value;
      if (pick("#tf-prompt")) node.prompt = { ...(node.prompt || {}), "zh-CN": pick("#tf-prompt").value };
      if (pick("#tf-question")) node.question = { ...(node.question || {}), "zh-CN": pick("#tf-question").value };
      if (pick("#tf-display-content")) node.content = { ...(node.content || {}), "zh-CN": pick("#tf-display-content").value };
      if (pick("#tf-failure-next")) node.failure_next = pick("#tf-failure-next").value;
      if (pick("#tf-end-state")) node.state = pick("#tf-end-state").value;
      if (pick("#tf-retryable")) node.retryable = pick("#tf-retryable").checked;
      if (pick("#tf-message")) node.message = { ...(node.message || {}), "zh-CN": pick("#tf-message").value };
      const timeout = pick("#tf-timeout")?.value.trim();
      if (timeout) { node.timeout_seconds = Number(timeout); node.timeout_next = pick("#tf-timeout-next").value; }
      else { delete node.timeout_seconds; delete node.timeout_next; }
      for (const key of ["fields", "outputs"]) {
        if (!pick("#tf-" + key + "-list")) continue;
        node[key] = (node[key] || []).map((definition, index) => {
          const prefix = "tf-" + key + "-" + index;
          const updated = { ...definition, key: pick("#" + prefix + "-key").value,
            label: { ...(definition.label || {}), "zh-CN": pick("#" + prefix + "-label").value },
            type: pick("#" + prefix + "-type").value,
            required: pick("#" + prefix + "-required").checked };
          if (updated.type === "select" && pick("#" + prefix + "-choices")) {
            updated.options = pick("#" + prefix + "-choices").value.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
              const split = line.indexOf("|");
              const value = (split < 0 ? line : line.slice(0, split)).trim();
              const label = (split < 0 ? line : line.slice(split + 1)).trim();
              const previous = (definition.options || []).find((option) => option.value === value);
              return { value, label: { ...(previous?.label || {}), "zh-CN": label } };
            });
          } else if (updated.type !== "select") delete updated.options;
          if (updated.type === "images") updated.max_items = Number(pick("#" + prefix + "-max")?.value || definition.max_items || 10);
          else delete updated.max_items;
          return updated;
        });
      }
      for (const [id, key, array] of [["tf-inputs", "inputs", false], ["tf-shown", "show_from", false], ["tf-result", "result", false]]) {
        const input = pick("#" + id);
        if (!input) continue;
        let value;
        try { value = JSON.parse(input.value || (array ? "[]" : "{}")); } catch { throw new Error(`${input.getAttribute?.("data-label") || key} 的 JSON 格式有误。`); }
        if (array ? !Array.isArray(value) : !value || Array.isArray(value) || typeof value !== "object") throw new Error(`${key} 需要${array ? "数组" : "对象"}。`);
        node[key] = value;
      }
      definition.entry = pick("#tf-entry")?.value || definition.entry;
    };
    const optionList = (value, empty = false) => `${empty ? '<option value="">不跳转</option>' : ""}${definition.nodes.map((node) => `<option value="${escape(node.id)}" ${node.id === value ? "selected" : ""}>${escape(text(node.label) || node.id)} · ${escape(node.id)}</option>`).join("")}`;
    const input = (id, label, value, type = "text", attributes = "") => `<div class="field"><label for="${id}">${label}</label><input id="${id}" type="${type}" value="${escape(value || "")}" ${disabled ? "disabled" : ""} ${attributes}></div>`;
    const area = (id, label, value, attributes = "") => `<div class="field"><label for="${id}">${label}</label><textarea id="${id}" data-label="${label}" ${disabled ? "disabled" : ""} ${attributes}>${escape(value || "")}</textarea></div>`;
    const json = (id, label, value, hint) => `<details class="task-flow-schema"><summary>${label}</summary>${area(id, label, JSON.stringify(value || {}, null, 2), 'spellcheck="false" class="mono"')}<p class="caption">${hint}</p></details>`;
    const schemaFields = (key, definitions, title) => `<div id="tf-${key}-list" class="task-flow-schema-fields"><div class="section-head"><h4>${title}</h4><button type="button" class="secondary" data-tf-add-field="${key}" ${disabled ? "disabled" : ""}>添加字段</button></div>${definitions.map((definition, index) => {
      const prefix = "tf-" + key + "-" + index;
      return `<div class="task-flow-field-row"><div class="grid">${input(prefix + "-label", "显示名称", text(definition.label))}${input(prefix + "-key", "字段标识", definition.key, "text", 'maxlength="64"')}</div><div class="grid"><div class="field"><label for="${prefix}-type">字段类型</label><select id="${prefix}-type" data-tf-field-type="${key}" ${disabled ? "disabled" : ""}>${[["text", "文本"], ["textarea", "多行文本"], ["select", "选择"], ["boolean", "是 / 否"], ["file", "文件"], ["image", "图片"], ["images", "图片集"], ["email", "邮箱"], ["url", "网址"], ["number", "数字"]].map(([value, label]) => `<option value="${value}" ${definition.type === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>${definition.type === "images" ? input(prefix + "-max", "最多图片数", definition.max_items || 10, "number", 'min="1" max="20"') : ""}</div>${definition.type === "select" ? area(prefix + "-choices", "选项（每行：固定值 | 显示名称）", (definition.options || []).map((option) => option.value + " | " + text(option.label)).join("\n")) : ""}<div class="section-head"><label><input id="${prefix}-required" type="checkbox" ${definition.required ? "checked" : ""} ${disabled ? "disabled" : ""}>必填</label><button type="button" class="danger" data-tf-remove-field="${key}:${index}" ${disabled ? "disabled" : ""}>删除字段</button></div></div>`;
    }).join("")}<details><summary>字段高级定义 · 教程与多语言</summary>${area("tf-" + key + "-json", "字段 JSON", JSON.stringify(definitions, null, 2), 'spellcheck="false" class="mono"')}<button type="button" class="secondary" data-tf-apply-fields="${key}" ${disabled ? "disabled" : ""}>应用字段 JSON</button></details></div>`;
    const draw = () => {
      if (!active()) return;
      const node = definition?.nodes.find((item) => item.id === selected) || definition?.nodes[0];
      if (node) selected = node.id;
      container.innerHTML = `<div class="section-head"><h3>任务编排（可选）</h3><label class="task-flow-enable"><input id="tf-enabled" type="checkbox" ${definition ? "checked" : ""} ${disabled ? "disabled" : ""}>启用多步交互</label></div><p class="caption">普通商品保持关闭即可。需要分轮提问、接收验证码、等待顾客确认或按选择跳转时，再启用。新定义只用于以后发行的卡密。启用后按每一步的填写项收集信息，最终交付仍须符合下方商品输出字段。</p><div class="task-flow-presets"><label for="tf-preset">快速开始</label><select id="tf-preset" ${disabled ? "disabled" : ""}><option value="">选择编排模板</option><option value="aladdin">阿拉丁神灯 · 回答三个问题</option><option value="confirmation">收集需求 → 处理 → 交付</option></select><button id="tf-use-preset" type="button" class="secondary" ${disabled ? "disabled" : ""}>使用模板</button></div>${definition ? `<div class="field"><label for="tf-entry">开始步骤</label><select id="tf-entry" ${disabled ? "disabled" : ""}>${optionList(definition.entry)}</select></div><div class="task-flow-editor-layout"><nav class="task-flow-node-list" aria-label="编排步骤">${definition.nodes.map((item, index) => `<button type="button" class="task-flow-node ${item.id === selected ? "selected" : "secondary"}" data-tf-node="${escape(item.id)}" aria-current="${item.id === selected ? "step" : "false"}"><span>${index + 1}. ${escape(text(item.label) || item.id)}</span><small>${{ input: "顾客填写", process: "处理", display: "展示与确认", end: "结束" }[item.kind]}</small></button>`).join("")}<div class="task-flow-add"><select id="tf-new-kind" aria-label="新步骤类型" ${disabled ? "disabled" : ""}><option value="input">顾客填写</option><option value="process">处理</option><option value="display">展示与确认</option><option value="end">结束</option></select><button type="button" id="tf-add" class="secondary" ${disabled ? "disabled" : ""}>添加步骤</button></div></nav><div class="task-flow-node-editor"><div class="section-head"><h4>${escape(text(node.label) || node.id)}</h4><button type="button" id="tf-remove" class="danger" ${disabled || definition.nodes.length < 2 ? "disabled" : ""}>删除步骤</button></div><p class="caption">${escape(node.id)} · ${{ input: "顾客填写", process: "处理", display: "展示与确认", end: "结束" }[node.kind]}</p>${input("tf-label", "步骤名称", text(node.label))}${node.kind === "input" ? `${area("tf-prompt", "开始前的提示", text(node.prompt))}${area("tf-question", "填写时的提示", text(node.question))}<div class="field"><label for="tf-start-policy">开始计时的时机</label><select id="tf-start-policy" ${disabled ? "disabled" : ""}><option value="confirm" ${node.start_policy !== "automatic" ? "selected" : ""}>顾客点击开始后</option><option value="automatic" ${node.start_policy === "automatic" ? "selected" : ""}>进入这一步时</option></select></div>${schemaFields("fields", node.fields || [], "顾客填写项")}` : ""}${node.kind === "display" ? area("tf-display-content", "展示说明", text(node.content)) : ""}${node.kind === "process" ? `${json("tf-inputs", "传给处理者的信息", node.inputs || {}, '例如 {"question":{"node":"question_1","field":"question"}}。只传明确引用的字段。')}${schemaFields("outputs", node.outputs || [], "本步必须返回的结果")}<div class="field"><label for="tf-failure-next">处理失败后跳转到</label><select id="tf-failure-next" ${disabled ? "disabled" : ""}>${optionList(node.failure_next, false)}</select></div>` : ""}${["input", "display"].includes(node.kind) ? json("tf-shown", "显示前面步骤的结果", node.show_from || {}, '例如 {"previous_answer":{"node":"answer_1","field":"answer"}}。只显示这里明确引用的结果。') : ""}${node.kind === "end" ? `<div class="field"><label for="tf-end-state">结束状态</label><select id="tf-end-state" ${disabled ? "disabled" : ""}>${[["succeeded", "兑换完成"], ["failed", "未完成"], ["rejected", "拒绝处理"]].map(([key, label]) => `<option value="${key}" ${node.state === key ? "selected" : ""}>${label}</option>`).join("")}</select></div>${area("tf-message", "结束说明", text(node.message))}<label><input id="tf-retryable" type="checkbox" ${node.retryable ? "checked" : ""} ${disabled ? "disabled" : ""}>失败后允许重试</label>${json("tf-result", "最终交付字段的来源", node.result || {}, '例如 {"content":{"node":"answer_3","field":"answer"}}。字段名须对应商品最终交付结构。')}` : `<div class="field"><label for="tf-next">完成后跳转到</label>${typeof node.next === "object" ? `<p class="caption">当前采用条件分支。通过下方高级定义编辑条件；这里不会覆盖原有分支。</p>` : `<select id="tf-next" ${disabled ? "disabled" : ""}>${optionList(node.next, false)}</select>`}</div><div class="grid">${input("tf-timeout", node.kind === "process" ? "本步超时（秒，处理步骤必须配置）" : "本步超时（秒，留空不设）", node.timeout_seconds, "number", 'min="1" max="86400" step="1"')}<div class="field"><label for="tf-timeout-next">超时后跳转到</label><select id="tf-timeout-next" ${disabled ? "disabled" : ""}>${optionList(node.timeout_next, true)}</select></div></div><p class="caption">期限由服务器计算。涉及付款或其他外部动作时，超时不能代表动作没有发生，应交给处理者核实。</p>`}</div></div><details id="tf-advanced" class="task-flow-advanced"><summary>高级定义 · 条件分支与完整 JSON</summary><p class="caption">条件只支持引用已定义字段并比较。不会运行代码、命令或表达式。</p>${area("tf-definition", "完整编排定义", JSON.stringify(definition, null, 2), 'class="mono" spellcheck="false"')}<button id="tf-apply-json" type="button" class="secondary" ${disabled ? "disabled" : ""}>应用 JSON 定义</button></details>` : `<p class="caption">当前使用简单兑换：一次提交需求，一次交付结果。</p>`}<div id="task-flow-editor-error" class="error" role="alert"></div>`;
      options.onChange?.(definition !== null);
      const on = (selector, event, callback) => pick(selector)?.addEventListener(event, (eventObject) => {
        if (!active() || disabled) return;
        try { callback(eventObject); changed = true; } catch (error) { notify(error); }
      });
      on("#tf-enabled", "change", () => { if (!pick("#tf-enabled").checked) { capture(); definition = null; } else definition = presets.confirmation(); selected = definition?.entry || ""; draw(); });
      on("#tf-use-preset", "click", () => { const preset = pick("#tf-preset").value; if (!preset) throw new Error("先选择一个编排模板。"); if (definition && !options.confirm?.("使用模板将替换当前编排定义。继续？") && options.confirm) return; const replacement = presets[preset](); options.onPreset?.(preset, clone(replacement)); definition = replacement; selected = definition.entry; draw(); });
      for (const button of container.querySelectorAll("[data-tf-node]")) button.addEventListener("click", () => { if (!active()) return; try { capture(); selected = button.dataset.tfNode; draw(); } catch (error) { notify(error); } });
      for (const button of container.querySelectorAll("[data-tf-add-field]")) button.addEventListener("click", () => { if (!active() || disabled) return; try { capture(); const key = button.dataset.tfAddField; const fields = node[key] || (node[key] = []); let index = fields.length + 1; while (fields.some((item) => item.key === "field_" + index)) index++; fields.push(field("field_" + index, "新字段", "text")); changed = true; draw(); } catch (error) { notify(error); } });
      for (const button of container.querySelectorAll("[data-tf-remove-field]")) button.addEventListener("click", () => { if (!active() || disabled) return; try { capture(); const [key, index] = button.dataset.tfRemoveField.split(":"); node[key].splice(Number(index), 1); changed = true; draw(); } catch (error) { notify(error); } });
      for (const control of container.querySelectorAll("[data-tf-field-type]")) control.addEventListener("change", () => { if (!active() || disabled) return; try { capture(); changed = true; draw(); } catch (error) { notify(error); } });
      for (const button of container.querySelectorAll("[data-tf-apply-fields]")) button.addEventListener("click", () => { if (!active() || disabled) return; try { const key = button.dataset.tfApplyFields; const value = JSON.parse(pick("#tf-" + key + "-json").value); if (!Array.isArray(value)) throw new Error("字段定义需要 JSON 数组。"); capture(); node[key] = value; changed = true; draw(); } catch (error) { notify(error); } });
      on("#tf-add", "click", () => {
        capture(); if (definition.nodes.length >= 64) throw new Error("最多配置 64 个步骤。");
        const kind = pick("#tf-new-kind").value;
        let count = definition.nodes.length + 1, id = `step_${count}`;
        while (definition.nodes.some((item) => item.id === id)) id = `step_${++count}`;
        let failure = definition.nodes.find((item) => item.kind === "end" && item.state === "failed");
        if (kind === "process" && !failure) {
          if (definition.nodes.length > 62) throw new Error("添加处理步骤还需要失败终点，最多配置 64 个步骤。");
          let failureId = "failed";
          while (definition.nodes.some((item) => item.id === failureId)) failureId += "_";
          failure = { id: failureId, kind: "end", state: "failed", retryable: true, message: { "zh-CN": "本次处理未完成。" } };
          definition.nodes.push(failure);
        }
        const newNode = { id, kind, label: { "zh-CN": "新步骤" }, ...(kind === "end" ? { state: "succeeded", result: {} } : { next: definition.nodes.find((item) => item.kind === "end")?.id || definition.entry }), ...(kind === "input" ? { fields: [field("text", "请填写")], start_policy: "confirm", prompt: {}, question: {} } : {}), ...(kind === "process" ? { inputs: {}, outputs: [field("content", "处理结果")], failure_next: failure.id, timeout_seconds: 3600, timeout_next: failure.id } : {}) };
        definition.nodes.push(newNode); selected = id; draw();
      });
      on("#tf-remove", "click", () => { capture(); if (definition.nodes.length < 2) return; const referenced = definition.nodes.some((item) => item.id !== selected && [item.next, item.timeout_next, item.failure_next].includes(selected)); if (referenced || definition.entry === selected) throw new Error("先修改开始步骤与指向此步骤的跳转，再删除。"); definition.nodes = definition.nodes.filter((item) => item.id !== selected); selected = definition.nodes[0].id; draw(); });
      on("#tf-apply-json", "click", () => { let value; try { value = JSON.parse(pick("#tf-definition").value); } catch { throw new Error("完整编排定义的 JSON 格式有误。"); } definition = validate(value); selected = definition?.entry || ""; draw(); });
      on("#tf-advanced", "toggle", () => { if (pick("#tf-advanced").open) { capture(); pick("#tf-definition").value = JSON.stringify(definition, null, 2); } });
    };
    draw();
    return { getValue() { if (!disabled) capture(); return validate(definition); }, hasChanges() { return changed; }, dispose() { definition = null; } };
  }
  window.ExtoreTaskFlowEditor = { mount, presets, validate };
})();
