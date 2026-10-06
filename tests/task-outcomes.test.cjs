const assert = require("node:assert/strict");
const test = require("node:test");
const { appFixture, flush, parameter, product, job, item, batch } = require("./app-fixture.cjs");

test("needs_input shows the reason, allows the server-authorized retry, and escapes saved parameters", async () => {
  const page = appFixture();
  const definition = { ...product("Needs details", [parameter("email"), parameter("notes", "textarea")]), allow_retry: false };
  const task = { ...job("task", "needs_input"), can_retry: true, message: "请补充订单号", params: { email: 'x\" onfocus=\"alert(1)', notes: "</textarea><script>x</script>" } };
  page.set({ currentBatch: null, currentProduct: definition, currentToken: "single-token" });
  page.navigate("/receipt#single-token");
  page.context.renderReceipt(task);
  assert.equal(page.node("#customer-message").textContent, "请补充订单号");
  assert.match(page.node("#app").innerHTML, /需要重试/);
  assert.match(page.node("#app").innerHTML, /检查信息并重试/);
  await page.node("#retry").emit("click");
  const markup = page.node("#app").innerHTML;
  assert.match(markup, /value="x&quot; onfocus=&quot;alert\(1\)"/);
  assert.match(markup, /&lt;\/textarea&gt;&lt;script&gt;x&lt;\/script&gt;/);
  assert.doesNotMatch(markup, /<script>/);
});

test("each needs_input card only prefills its own snapshot and retains its prior file ID", async () => {
  const page = appFixture();
  const a = item("A", product("A", [parameter("answer"), parameter("attachment", "file")]), {
    ...job("A", "needs_input"), can_retry: true, params: { answer: "A-only", attachment: "file-A" }, message: "补充 A",
  });
  const b = item("B", product("B", [parameter("answer")]), { ...job("B", "needs_input"), can_retry: true, params: { answer: "B-only" }, message: "补充 B" });
  batch(page, [a, b]);
  page.context.openBatchCard(a);
  await page.node("#retry").emit("click");
  const markup = page.node("#app").innerHTML;
  assert.match(markup, /param-A-answer[^>]*value="A-only"/);
  assert.doesNotMatch(markup, /B-only|param-B-/);
  assert.match(markup, /id="param-A-attachment-retained" type="hidden" value="file-A"/);
  assert.doesNotMatch(markup, /id="param-A-attachment" type="file" required/);
  page.node("#param-A-answer").value = "A-updated";
  page.node("#param-A-attachment-retained").value = "file-A";
  const submitting = page.node("#form").emit("submit");
  assert.equal(page.requests[0].url, "/api/redeem");
  assert.deepEqual(page.requests[0].body.items, [{ card_id: "A", params: { answer: "A-updated", attachment: "file-A" } }]);
  page.requests[0].respond({ batch: true, product: product(), items: [{ ...a, job: job("A", "queued") }, b] });
  await submitting;
});

test("rejected receipts expose the reason and have no retry, reveal, or destroy action", () => {
  const page = appFixture();
  page.set({ currentProduct: product("Rejected"), currentBatch: null });
  page.context.renderReceipt({ ...job("rejected", "rejected"), message: "此需求不在服务范围", can_retry: false });
  assert.equal(page.node("#customer-message").textContent, "此需求不在服务范围");
  assert.match(page.node("#app").innerHTML, /已拒绝/);
  assert.doesNotMatch(page.node("#app").innerHTML, /id="(?:retry|reveal|destroy)"/);
});

async function processingQueue(claimedBy = "owner") {
  const page = appFixture();
  page.navigate("/admin");
  page.set({ tab: "jobs", role: "admin" });
  const rendering = page.context.renderJobs("", "product");
  page.requests[0].respond([product()]);
  await flush();
  page.requests[1].respond([{ ...job("task", "processing"), claimed_by: claimedBy, params: {}, files: [] }]);
  await rendering;
  page.collections.set("[name=job]:checked", [{ value: "task" }]);
  return page;
}

test("request_retry and reject require a bounded reason and only affect owned processing tasks", async () => {
  for (const [button, action] of [["#request-changes", "request_retry"], ["#reject", "reject"]]) {
    const page = await processingQueue();
    await page.node(button).emit("click");
    for (const reason of ["   ", "x".repeat(1001)]) {
      page.node("#disposition-reason").value = reason;
      await page.node("#disposition-submit").emit("click");
      assert.equal(page.requests.length, 2);
      assert.match(page.node("#error").textContent, /填写原因/);
    }
    page.node("#disposition-reason").value = "  请提供订单号  ";
    const submitting = page.node("#disposition-submit").emit("click");
    assert.deepEqual(page.requests[2].body, { product_id: "product", ids: ["task"], action, message: "请提供订单号", ...(action === "request_retry" ? { reason_type: "customer_input", retry_mode: "revise" } : {}) });
    page.requests[2].respond({ ok: true });
    await flush();
    page.requests[3].respond([product()]);
    await flush();
    page.requests[4].respond([]);
    await submitting;
  }
  const other = await processingQueue("another-link");
  await other.node("#request-changes").emit("click");
  assert.match(other.node("#error").textContent, /你已领取/);
  assert.equal(other.requests.length, 2);
});

test("permanent rejection asks for confirmation and cancellation sends no mutation", async () => {
  const page = await processingQueue();
  await page.node("#reject").emit("click");
  page.node("#disposition-reason").value = "不提供该服务";
  let confirms = 0;
  page.context.confirm = () => { confirms++; return false; };
  await page.node("#disposition-submit").emit("click");
  assert.equal(confirms, 1);
  assert.equal(page.requests.length, 2);
});

test("uploads fetch the public limit once and reject oversize files before sending a body", async () => {
  const page = appFixture({ uploadLimit: null });
  const label = page.node("upload-limit-label");
  page.collections.set("[data-upload-limit]", [label]);
  const uploading = page.context.uploadMultipart("/files/upload", { token: "token" }, { size: 3 * 1024 * 1024 });
  assert.equal(page.requests[0].url, "/api/upload-limits");
  assert.equal(page.requests[0].body, undefined);
  page.requests[0].respond({ max_file_bytes: 2 * 1024 * 1024 });
  await assert.rejects(uploading, /2 MiB/);
  assert.equal(page.requests.length, 1);
  assert.match(label.textContent, /2 MiB/);
  await assert.rejects(page.context.uploadMultipart("/files/upload", {}, { size: 3 * 1024 * 1024 }), /2 MiB/);
  assert.equal(page.requests.length, 1);
});

test("upload-limit lookup failure retains the advertised 20 MiB fallback", async () => {
  const page = appFixture({ uploadLimit: null });
  const uploading = page.context.uploadMultipart("/files/upload", {}, { size: 21 * 1024 * 1024 });
  page.requests[0].reject(new Error("offline"));
  await assert.rejects(uploading, /20 MiB/);
  assert.equal(page.requests.length, 1);
});

test("reuse receipts wait for an explicit click, send only the selected card, and ignore a late response", async () => {
  const page = appFixture();
  const a = item("A", product("A", [parameter("answer")]), { ...job("A", "needs_input"), can_retry: true, retry_mode: "reuse", params: { answer: "original-private-answer" } });
  const b = item("B", product("B"), { ...job("B", "needs_input"), can_retry: true, retry_mode: "revise" });
  batch(page, [a, b]); page.context.openBatchCard(a);
  assert.match(page.node("#app").innerHTML, /使用原资料重试/);
  assert.doesNotMatch(page.node("#app").innerHTML, /original-private-answer|id="form"/);
  assert.equal(page.requests.length, 0);
  const retry = page.node("#retry").emit("click");
  assert.equal(page.requests[0].url, "/api/retry");
  assert.deepEqual(page.requests[0].body, { token: "batch-token", card_id: "A" });
  page.context.openBatchCard(b);
  page.requests[0].respond(job("A", "queued")); await retry;
  assert.match(page.node("#app").innerHTML, /<h1>B<\/h1>/);
  assert.match(page.node("#app").innerHTML, /检查信息并重试/);
  assert.equal(a.job.state, "needs_input");
  assert.equal(page.requests.length, 1);
});

test("external reasons keep revise unless the operator explicitly permits reuse", async () => {
  for (const mode of ["revise", "reuse"]) {
    const page = await processingQueue();
    await page.node("#request-changes").emit("click");
    page.node("#disposition-reason").value = "外部平台恢复后请重试";
    page.node("#retry-reason-kind").value = "external";
    if (mode === "reuse") page.node("#retry-mode").value = "reuse";
    const submitting = page.node("#disposition-submit").emit("click");
    assert.equal(page.requests[2].body.reason_type, "external");
    assert.equal(page.requests[2].body.retry_mode, mode);
    assert.equal(page.requests[2].body.message, "外部平台恢复后请重试");
    page.requests[2].respond({ ok: true }); await flush();
    page.requests[3].respond([product()]); await flush(); page.requests[4].respond([]); await submitting;
  }
});
