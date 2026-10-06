"use strict";

(() => {
  const views = new WeakMap();
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const localized = (value, lang) => typeof value === "object" && value !== null ? value[lang] || value["zh-CN"] || Object.values(value)[0] || "" : value || "";
  const time = (value) => typeof value === "number" ? value * (value < 100000000000 ? 1000 : 1) : Date.parse(value || "");
  const remaining = (deadline, serverTime, now = Date.now()) => Number.isFinite(time(deadline)) ? Math.max(0, Math.ceil((time(deadline) - (Number.isFinite(time(serverTime)) ? time(serverTime) : now)) / 1000)) : null;
  const clock = (seconds) => seconds === null ? "" : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  const terminal = (flow) => !flow || flow.phase === "ended";
  const flowOf = (job, product) => job?.task_flow || product?.task_flow_view || (product?.task_flow?.enabled ? product.task_flow : null);

  function control(field, lang) {
    const id = "task-flow-field-" + field.key;
    const required = field.required ? "required" : "";
    let markup;
    if (window.ExtoreFields?.control) markup = window.ExtoreFields.control(id, field, "");
    else if (field.type === "select" || field.type === "boolean") {
      const options = field.type === "boolean"
        ? [{ value: "true", label: lang === "en" ? "Yes" : "是" }, { value: "false", label: lang === "en" ? "No" : "否" }]
        : field.options || [];
      markup = `<select id="${escape(id)}" ${required}><option value="">${lang === "en" ? "Choose…" : "请选择"}</option>${options.map((option) => `<option value="${escape(option.value)}">${escape(localized(option.label, lang))}</option>`).join("")}</select>`;
    } else if (["file", "image", "images"].includes(field.type)) {
      markup = `<input id="${escape(id)}" type="file" ${required} ${field.type === "images" ? "multiple" : ""} ${field.type !== "file" ? 'accept="image/png,image/jpeg,image/webp,image/gif"' : ""}>`;
    } else if (field.type === "textarea") markup = `<textarea id="${escape(id)}" ${required} maxlength="10000"></textarea>`;
    else markup = `<input id="${escape(id)}" ${field.sensitive ? "data-task-flow-sensitive" : ""} type="${field.sensitive ? "password" : escape(["text", "email", "url", "number"].includes(field.type) ? field.type : "text")}" ${required} ${field.type === "number" ? 'step="any"' : ""} maxlength="10000" ${field.sensitive ? 'autocomplete="off"' : ""}>`;
    if (field.sensitive) markup = markup.replace(/<(input|textarea)\b/, "<$1 data-task-flow-sensitive");
    const description = localized(field.description, lang);
    const help = description ? `<details ${field.collapsed ? "" : "open"}><summary>${lang === "en" ? "Instructions" : "填写说明"}</summary><div class="markdown">${window.DOMPurify?.sanitize && window.marked?.parse ? window.DOMPurify.sanitize(window.marked.parse(description), { FORBID_TAGS: ["img", "style", "iframe", "form", "input"], FORBID_ATTR: ["style", "id"] }) : escape(description)}</div></details>` : "";
    return `<div class="field"><label for="${escape(id)}">${escape(localized(field.label, lang) || field.key)}${field.required ? " *" : ""}</label>${markup}</div>${help}`;
  }

  function render(ctx) {
    const flow = ctx.flow || flowOf(ctx.job, ctx.product);
    if (terminal(flow)) { dispose(ctx.app); return false; }
    const tr = (cn, en) => ctx.lang === "en" ? en : cn;
    const key = JSON.stringify([ctx.token, ctx.cardId || "", ctx.lang, flow.flow_epoch, flow.phase, flow.current?.id]);
    let view = views.get(ctx.app);
    if (view?.key === key && view.root?.isConnected !== false && ctx.app.querySelector("#task-flow-root")) {
      view.flow = flow;
      view.ctx = ctx;
      update(view);
      return true;
    }
    dispose(ctx.app);
    view = { key, flow, ctx, timer: null, expiredRefresh: false, disposed: false, busy: false };
    views.set(ctx.app, view);
    const current = flow.current || {};
    const phase = flow.phase;
    const activeInput = phase === "input";
    const awaiting = phase === "await_start";
    const processing = ["queued", "processing"].includes(phase);
    const action = awaiting ? "start" : activeInput ? "answer" : phase === "display" ? "continue" : null;
    const title = localized(current.label, ctx.lang) || tr("兑换步骤", "Redemption step");
    const shown = (flow.shown || []).map((item) => `<section class="task-flow-answer"><h3>${escape(localized(item.label, ctx.lang) || item.key || tr("上一步的结果", "Previous result"))}</h3><pre class="result">${escape(item.value)}</pre></section>`).join("");
    const prompt = localized(awaiting ? current.prompt : current.question || current.prompt, ctx.lang);
    const batchBack = ctx.cardId ? `<button type="button" id="task-flow-batch-back" class="secondary">← ${tr("全部卡密", "All codes")}</button>` : "";
    ctx.app.innerHTML = `<div id="task-flow-root" class="narrow task-flow-page">${batchBack}<h1>${escape(ctx.product.name)}</h1>${ctx.variant?.name ? `<p class="caption">${tr("规格：", "Variant: ")}${escape(ctx.variant.name)}</p>` : ""}<section class="panel receipt-panel ${processing ? "receipt-waiting" : ""}">${processing ? '<div class="paper-divider waiting-top" aria-hidden="true"></div>' : ""}<div class="section-head"><h2>${escape(title)}</h2><span id="task-flow-status" class="status"></span></div><p id="task-flow-timing" class="task-flow-timing" hidden><span>${tr("本步剩余时间", "Time left for this step")}</span><strong id="task-flow-clock" class="mono"></strong></p>${shown}${prompt ? `<p class="task-flow-prompt">${escape(prompt)}</p>` : ""}<p id="task-flow-message"></p><p id="task-flow-position" class="caption" hidden></p>${activeInput ? `<form id="task-flow-form">${(current.fields || []).map((field) => control(field, ctx.lang)).join("")}<button id="task-flow-submit" type="submit" class="full">${tr("提交，继续下一步", "Submit and continue")}</button></form>` : action ? `<button id="task-flow-submit" type="button" class="full">${awaiting ? tr("开始", "Start") : tr("继续", "Continue")}</button>` : ""}${awaiting ? `<p class="caption">${tr("点击开始后，本步骤才开始计时。刷新页面不会重新计时。", "This step's timer starts when you click Start. Refreshing the page does not reset it.")}</p>` : ""}${processing ? `<div class="task-flow-process-line"><span aria-hidden="true"></span><p class="caption">${tr("正在处理当前步骤，结果会自动出现在这里。", "This step is being processed. Its result will appear here automatically.")}</p></div>` : ""}<div id="task-flow-error" class="error" role="alert"></div>${processing ? '<div class="paper-divider waiting-bottom" aria-hidden="true"></div>' : ""}</section><div class="receipt-link"><strong>${tr("保存领取链接", "Save your receipt link")}</strong><p>${escape(ctx.receiptUrl)}</p><button type="button" id="task-flow-copy" class="secondary">${tr("复制链接", "Copy link")}</button><p class="caption">${tr("可以从同一个领取链接继续当前步骤，请勿转发给他人。", "Use this receipt link to return to the current step. Do not forward it.")}</p></div></div>`;
    view.root = ctx.app.querySelector("#task-flow-root");
    const active = () => !view.disposed && views.get(ctx.app) === view && view.ctx.active() && view.root?.isConnected !== false;
    const execute = async () => {
      if (!active() || view.busy) return;
      view.busy = true;
      const button = ctx.app.querySelector("#task-flow-submit");
      if (button) button.disabled = true;
      const error = ctx.app.querySelector("#task-flow-error");
      if (error) error.textContent = "";
      try {
        const currentFlow = view.flow;
        if (!(currentFlow.actions || []).includes(action)) throw new Error(tr("当前步骤已经变化，请刷新后继续。", "The step has changed. Refresh to continue."));
        const body = { token: ctx.token, flow_epoch: currentFlow.flow_epoch, expected_revision: currentFlow.revision };
        if (ctx.cardId) body.card_id = ctx.cardId;
        if (action === "answer") {
          body.values = {};
          for (const definition of currentFlow.current?.fields || []) {
            const node = ctx.app.querySelector("#task-flow-field-" + definition.key);
            if (!node) throw new Error(tr("填写项已变化，请刷新后继续。", "The fields have changed. Refresh to continue."));
            if (["file", "image", "images"].includes(definition.type)) {
              const chosen = Array.from(node.files || []);
              if (definition.type !== "images" && chosen.length > 1) throw new Error(tr("这个填写项只能上传一个文件。", "This field accepts one file."));
              if (definition.type === "images" && chosen.length > (definition.max_items || 10)) throw new Error(tr("选择的图片数量超过这个填写项的限制。", "Too many images for this field."));
              const ids = [];
              for (const file of chosen) {
                const upload = { token: ctx.token, field_key: definition.key, flow_epoch: currentFlow.flow_epoch, expected_revision: currentFlow.revision, node_id: currentFlow.current.id };
                if (ctx.cardId) upload.card_id = ctx.cardId;
                ids.push((await ctx.upload("/files/upload", upload, file)).id);
                if (!active()) return;
              }
              body.values[definition.key] = definition.type === "images" ? JSON.stringify(ids) : ids[0] || "";
            } else body.values[definition.key] = window.ExtoreFields?.read ? window.ExtoreFields.read(node, definition) : node.value;
          }
        }
        const result = await ctx.api("/task-flow/" + action, body);
        if (active()) await view.ctx.onResult(result);
      } catch (failure) {
        if (active() && error) error.textContent = failure.message || String(failure);
      } finally {
        view.busy = false;
        if (active() && button) button.disabled = false;
      }
    };
    const form = ctx.app.querySelector("#task-flow-form");
    if (form) form.addEventListener("submit", (event) => { event.preventDefault(); void execute(); });
    else ctx.app.querySelector("#task-flow-submit")?.addEventListener("click", () => { void execute(); });
    ctx.app.querySelector("#task-flow-copy")?.addEventListener("click", async () => {
      const copied = await ctx.copy(ctx.receiptUrl);
      if (active()) ctx.notify(copied ? tr("链接已复制", "Link copied") : tr("复制失败，请手动复制上方链接。", "Could not copy. Copy the link above manually."));
    });
    ctx.app.querySelector("#task-flow-batch-back")?.addEventListener("click", () => { if (active()) ctx.back?.(); });
    update(view);
    const tick = () => {
      if (!active()) { dispose(ctx.app, view); return; }
      update(view);
      view.timer = setTimeout(tick, document.hidden ? 15000 : 1000);
    };
    view.timer = setTimeout(tick, 1000);
    if ((current.fields || []).some((field) => ["file", "image", "images"].includes(field.type))) void ctx.uploadLimit?.();
    return true;
  }

  function update(view) {
    const { flow, ctx } = view;
    const tr = (cn, en) => ctx.lang === "en" ? en : cn;
    // Calculate against the server's clock offset, never against poll age or a new duration.
    if (view.serverStamp !== flow.server_time) {
      view.serverStamp = flow.server_time;
      view.offset = Number.isFinite(time(flow.server_time)) ? time(flow.server_time) - Date.now() : 0;
    }
    const seconds = remaining(flow.deadline, null, Date.now() + (view.offset || 0));
    const timing = ctx.app.querySelector("#task-flow-timing");
    if (timing) timing.hidden = seconds === null;
    const clockNode = ctx.app.querySelector("#task-flow-clock");
    if (clockNode) clockNode.textContent = seconds === 0 ? tr("时间已到，正在同步", "Time is up. Syncing…") : clock(seconds);
    const status = ctx.app.querySelector("#task-flow-status");
    if (status) status.textContent = ({ await_start: tr("等待开始", "Ready to start"), input: tr("等待填写", "Your input"), queued: tr("排队中", "Queued"), processing: tr("处理中", "Processing"), display: tr("查看结果", "Review result") })[flow.phase] || "";
    const message = ctx.app.querySelector("#task-flow-message");
    if (message) message.textContent = ctx.job?.message || "";
    const position = ctx.app.querySelector("#task-flow-position");
    if (position) {
      position.hidden = flow.phase !== "queued";
      const place = ctx.job?.queue_position || (Number(ctx.job?.queue_ahead) || 0) + 1;
      position.textContent = tr(`当前队列第 ${place} 位`, `Queue position ${place}`);
    }
    if (seconds === 0 && !view.expiredRefresh && !view.busy) {
      view.expiredRefresh = true;
      Promise.resolve(ctx.refresh()).catch(() => { view.expiredRefresh = false; });
    }
  }

  function dispose(app, expected) {
    const view = views.get(app);
    if (!view || (expected && expected !== view)) return;
    view.disposed = true;
    clearTimeout(view.timer);
    for (const input of app.querySelectorAll?.('[data-task-flow-sensitive]') || []) input.value = "";
    views.delete(app);
  }
  window.ExtoreTaskFlow = { render, dispose, flowOf, remaining, clock };
})();
