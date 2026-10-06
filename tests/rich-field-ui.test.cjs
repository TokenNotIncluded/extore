const assert = require("node:assert/strict");
const test = require("node:test");
const { File } = require("node:buffer");
const { appFixture, flush, parameter, product, job, item, batch } = require("./app-fixture.cjs");

const first = "11111111-1111-4111-8111-111111111111";
const second = "22222222-2222-4222-8222-222222222222";
const field = (key = "pictures", maximum = 2) => ({ ...parameter(key, "images"), max_items: maximum });
const picture = (name) => new File(["image bytes"], name + ".png", { type: "image/png" });
function customer(fields) {
  const page = appFixture();
  page.set({ currentToken: "batch-token", currentProduct: product("Pictures", fields), currentJob: null });
  page.context.redemptionForm();
  return page;
}

test("rich fields render grouped file picking and localized stable choices", () => {
  const page = customer([
    field(), parameter("cover", "image"), parameter("confirm", "boolean"),
    { ...parameter("tier", "select"), options: [{ value: "basic", label: { "zh-CN": '<基础版>' } }] },
  ]);
  const markup = page.node("#app").innerHTML;
  assert.match(markup, /id="param-pictures" type="file" multiple accept="image\/png,image\/jpeg,image\/webp"/);
  assert.match(markup, /最多 2 张图片/);
  assert.match(markup, /id="param-cover" type="file"\s+accept=/);
  assert.match(markup, /value="false"\s*>否/);
  assert.match(markup, /value="basic"\s*>&lt;基础版&gt;/);
  assert.doesNotMatch(markup, /type="(?:boolean|select|images|image)"/);
});

test("boolean false remains a string and choice labels never replace stable values", async () => {
  const page = customer([parameter("confirm", "boolean"), { ...parameter("tier", "select"), options: [{ value: "basic", label: { "zh-CN": "基础版" } }] }]);
  page.node("#param-confirm").value = "false";
  page.node("#param-tier").value = "basic";
  const submitting = page.node("#form").emit("submit");
  assert.deepEqual(page.requests[0].body.params, { confirm: "false", tier: "basic" });
  page.requests[0].respond(job("job", "queued"));
  await submitting;
});

test("customer image collections upload sequentially and submit opaque IDs as a JSON string", async () => {
  const page = customer([field()]);
  page.node("#param-pictures").files = [picture("one"), picture("two")];
  const submitting = page.node("#form").emit("submit");
  await flush();
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].body.field_key, "pictures");
  assert.equal(page.requests[0].body.token, "batch-token");
  page.requests[0].respond({ id: first });
  await flush();
  assert.equal(page.requests[1].body.file.name, "two.png");
  page.requests[1].respond({ id: second });
  await flush();
  assert.equal(page.requests[2].url, "/api/redeem");
  assert.deepEqual(page.requests[2].body.params, { pictures: JSON.stringify([first, second]) });
  page.requests[2].respond(job("job", "queued"));
  await submitting;
});

test("an excessive collection fails before allocating upload storage", async () => {
  const page = customer([field("pictures", 1)]);
  page.node("#param-pictures").files = [picture("one"), picture("two")];
  await page.node("#form").emit("submit");
  assert.equal(page.requests.length, 0);
  assert.match(page.node("#error").textContent, /数量超过/);
});

test("optional empty image collections remain canonical empty JSON lists", async () => {
  const page = customer([{ ...field(), required: false }]);
  const submitting = page.node("#form").emit("submit");
  await flush();
  assert.deepEqual(page.requests[0].body.params, { pictures: "[]" });
  page.requests[0].respond(job("job", "queued"));
  await submitting;
});

test("retry retains image IDs without uploading again", async () => {
  const page = customer([field()]);
  page.set({ currentJob: { ...job("job", "needs_input"), params: { pictures: JSON.stringify([first, second]) } } });
  page.context.redemptionForm();
  assert.match(page.node("#app").innerHTML, /已保留 2 个附件/);
  page.node("#param-pictures-retained").value = JSON.stringify([first, second]);
  const submitting = page.node("#form").emit("submit");
  await flush();
  assert.equal(page.requests[0].url, "/api/redeem");
  assert.deepEqual(page.requests[0].body.params, { pictures: JSON.stringify([first, second]) });
  page.requests[0].respond(job("job", "queued"));
  await submitting;
});

test("batch copy skips image attachments and incompatible select values", async () => {
  const page = appFixture();
  const option = (value) => ({ ...parameter("tier", "select"), options: [{ value, label: { "zh-CN": value } }] });
  const a = item("A", product("A", [field(), parameter("cover", "image"), option("basic"), parameter("confirm", "boolean")]), null);
  const b = item("B", product("B", [field(), parameter("cover", "image"), option("pro"), parameter("confirm", "boolean")]), null);
  page.context.openBatch(batch(page, [a, b]));
  page.node("#param-A-pictures").value = "never copy files";
  page.node("#param-A-cover").value = "never copy images";
  page.node("#param-A-tier").value = "basic";
  page.node("#param-B-tier").value = "pro";
  page.node("#param-A-confirm").value = "false";
  await page.node("#copy-params").emit("click");
  assert.equal(page.node("#param-B-pictures").value, "");
  assert.equal(page.node("#param-B-cover").value, "");
  assert.equal(page.node("#param-B-tier").value, "pro");
  assert.equal(page.node("#param-B-confirm").value, "false");
});

test("switching receipts during the first upload cancels all remaining uploads and redemption", async () => {
  const page = customer([field()]);
  page.node("#param-pictures").files = [picture("one"), picture("two")];
  const submitting = page.node("#form").emit("submit");
  await flush();
  page.navigate("/receipt#another-token");
  page.requests[0].respond({ id: first });
  await submitting;
  assert.equal(page.requests.length, 1);
});

const outputFile = (id, extra = {}) => ({ id, job_id: "job-A", kind: "output", field_key: "pictures", attempt: 2, available: true, consumed: false, filename: id + ".png", content_type: "image/png", size: 2048, ...extra });
async function queue(files = []) {
  const definition = { ...product(), outputs: [field()] };
  const task = { ...job("job-A", "processing"), product_id: "product", claimed_by: "owner", attempt: 2, files, outputs: definition.outputs };
  const page = appFixture();
  page.navigate("/admin");
  page.set({ tab: "jobs", role: "admin" });
  const rendering = page.context.renderJobs("", "product");
  page.requests[0].respond([definition]);
  await flush();
  page.requests[1].respond([task]);
  await rendering;
  page.collections.set("[name=job]:checked", [{ value: task.id }]);
  const opening = page.node("#complete").emit("click");
  page.requests[2].respond([task]);
  await opening;
  return page;
}
async function finishRefresh(page, index) {
  await flush();
  page.requests[index].respond([product()]);
  await flush();
  page.requests[index + 1].respond([]);
}

test("image collection delivery selects current-task drafts and preserves list order", async () => {
  const page = await queue([outputFile(first), outputFile(second), outputFile("other", { attempt: 1 })]);
  assert.match(page.node("#batch-form").innerHTML, /name="batch-uploaded-0"/);
  assert.doesNotMatch(page.node("#batch-form").innerHTML, /value="other"/);
  page.collections.set('[name="batch-uploaded-0"]', [{ value: second, checked: true }, { value: first, checked: true }]);
  const submitting = page.node("#batch-submit").emit("click");
  await flush();
  assert.deepEqual(page.requests[3].body.output, { pictures: JSON.stringify([second, first]) });
  page.requests[3].respond({ ok: true });
  await finishRefresh(page, 4);
  await submitting;
});

test("image collection delivery refuses drafts from another task or too many files", async () => {
  for (const ids of [[first, second, "33333333-3333-4333-8333-333333333333"], ["44444444-4444-4444-8444-444444444444"]]) {
    const page = await queue([outputFile(first), outputFile(second), outputFile(ids[2] || first)]);
    page.collections.set('[name="batch-uploaded-0"]', ids.map((value) => ({ value, checked: true })));
    await page.node("#batch-submit").emit("click");
    assert.equal(page.requests.length, 3);
    assert.match(page.node("#error").textContent, /无效|不属于/);
  }
});

test("a cancelled collection upload cannot upload its second image or complete the task", async () => {
  const page = await queue();
  page.node("#batch-output-0").files = [picture("one"), picture("two")];
  const submitting = page.node("#batch-submit").emit("click");
  await flush();
  assert.equal(page.requests[3].url, "/api/manage/files/upload");
  await page.node("#batch-cancel").emit("click");
  page.requests[3].respond({ id: first });
  await submitting;
  assert.equal(page.requests.length, 4);
});

test("image delivery previews use authenticated blobs and share one download with once-policy saving", async () => {
  const page = customer([]);
  page.set({ currentProduct: { ...product(), outputs: [field()], view_policy: "once" } });
  const revealing = page.context.revealReceipt();
  page.requests[0].respond({ output: { pictures: JSON.stringify([first, second]) }, files: [outputFile(first), outputFile(second)] });
  await revealing;
  assert.match(page.node("#content").innerHTML, /查看图片/);
  assert.doesNotMatch(page.node("#content").innerHTML, /<img|src="https?:/);
  assert.equal(page.requests.length, 1, "rendering must not consume file views");
  const previewing = page.node(`[data-delivery-image="${first}"]`).emit("click");
  assert.equal(page.requests[1].url, "/api/files/download");
  assert.equal(page.requests[1].body.file_id, first);
  page.requests[1].respond({}, new Blob(["png"], { type: "application/octet-stream" }));
  await previewing;
  assert.match(page.node(`[data-delivery-preview="${first}"]`).innerHTML, /src="blob:test-download"/);
  await page.node(`[data-delivery-file="${first}"]`).emit("click");
  assert.equal(page.requests.length, 2, "saving an already viewed image must reuse its local blob");
  assert.equal(page.downloads.length, 1);
});

test("a late image preview never appears on a different receipt", async () => {
  const page = customer([]);
  const previewing = page.context.previewDeliveryImage(outputFile(first));
  page.navigate("/receipt#someone-else");
  page.node(`[data-delivery-preview="${first}"]`).innerHTML = "New receipt";
  page.requests[0].respond({});
  await previewing;
  assert.equal(page.node(`[data-delivery-preview="${first}"]`).innerHTML, "New receipt");
});


test("an upload-limits response cannot send WebMCP attachments after receipt change or abort", async () => {
  for (const abort of [false, true]) {
    const page = appFixture({ uploadLimit: null });
    const controller = new AbortController();
    page.set({ currentToken: "batch-token", currentProduct: product("Images", [field()]) });
    const uploading = page.context.uploadFile({ scope: "customer", field_key: "pictures", filename: "picture.png", content_type: "image/png", base64: btoa("png") }, { signal: controller.signal });
    const rejected = assert.rejects(uploading, /页面已切换|尚未提交|上下文/);
    assert.equal(page.requests[0].url, "/api/upload-limits");
    if (abort) controller.abort();
    else page.navigate("/receipt#another-token");
    page.requests[0].respond({ max_file_bytes: 1048576 });
    await rejected;
    assert.equal(page.requests.length, 1);
  }
});


test("choice and boolean deliveries show translated labels while preserving plain text", async () => {
  const page = customer([]);
  page.set({ currentProduct: { ...product(), outputs: [parameter("confirm", "boolean"), { ...parameter("tier", "select"), options: [{ value: "basic", label: { "zh-CN": "基础版" } }] }, parameter("remark")] } });
  const revealing = page.context.revealReceipt();
  page.requests[0].respond({ output: { confirm: "false", tier: "basic", remark: "保留文本 <tag>" } });
  await revealing;
  assert.match(page.node("#content").innerHTML, /class="result">否<\/pre>/);
  assert.match(page.node("#content").innerHTML, /class="result">基础版<\/pre>/);
  assert.match(page.node("#content").innerHTML, /保留文本 &lt;tag&gt;/);
});
