const assert = require("node:assert/strict");
const test = require("node:test");
const { File } = require("node:buffer");
const { appFixture, flush, parameter, product, job } = require("./app-fixture.cjs");

const task = (files = []) => ({ ...job("job-A", "processing"), product_id: "product", claimed_by: "owner", attempt: 2, params: {}, files, outputs: [parameter("document", "file")] });
const attachment = (id, extra = {}) => ({ id, job_id: "job-A", kind: "output", field_key: "document", attempt: 2, available: true, consumed: false, filename: id + ".pdf", size: 2048, ...extra });

async function queue(row = task()) {
  const page = appFixture();
  page.navigate("/admin");
  page.set({ tab: "jobs", role: "admin" });
  const rendering = page.context.renderJobs("", "product");
  page.requests[0].respond([{ ...product(), outputs: row.outputs }]);
  await flush();
  page.requests[1].respond([row]);
  await rendering;
  page.collections.set("[name=job]:checked", [{ value: row.id }]);
  return page;
}
async function open(page, current) {
  const count = page.requests.length;
  const opening = page.node("#complete").emit("click");
  assert.match(page.requests[count].url, /job_id=job-A/);
  page.requests[count].respond([current]);
  await opening;
}
async function finishRefresh(page, count) {
  await flush();
  page.requests[count].respond([product()]);
  await flush();
  page.requests[count + 1].respond([]);
}

test("fresh output drafts can be reviewed and selected without uploading again", async () => {
  const page = await queue();
  const draft = attachment("server-draft", { filename: '<img src=x onerror="x">.pdf' });
  await open(page, task([draft]));
  const markup = page.node("#batch-form").innerHTML;
  assert.match(markup, /已上传附件/);
  assert.match(markup, /value="server-draft"/);
  assert.match(markup, /2 KiB/);
  assert.match(markup, /检查附件：&lt;img/);
  assert.doesNotMatch(markup, /<img/);
  assert.equal(page.requests.length, 3, "opening a dialog must not complete the task");
  page.node("#batch-uploaded-0").value = "server-draft";
  await page.node("#batch-uploaded-0").emit("change");
  assert.equal(page.node("#batch-output-0").required, false);
  assert.match(page.node("#batch-uploaded-id-0").textContent, /server-draft/);
  page.node("#batch-message").value = "检查后交付";
  const submitting = page.node("#batch-submit").emit("click");
  assert.equal(page.requests[3].url, "/api/manage/batch");
  assert.deepEqual(page.requests[3].body.output, { document: "server-draft" });
  assert.equal(page.requests.filter((request) => request.url.includes("/files/upload")).length, 0);
  page.requests[3].respond({ ok: true });
  await finishRefresh(page, 4);
  await submitting;
});

test("draft choices exclude other jobs, input files, fields, attempts and consumed files", async () => {
  const page = await queue();
  await open(page, task([
    attachment("valid"), attachment("cross-job", { job_id: "job-B" }),
    attachment("input-file", { kind: "input" }), attachment("other-field", { field_key: "other" }),
    attachment("old-attempt", { attempt: 1 }), attachment("consumed", { consumed: true }),
    attachment("unavailable", { available: false }),
  ]));
  const markup = page.node("#batch-form").innerHTML;
  assert.match(markup, /value="valid"/);
  assert.doesNotMatch(markup, /cross-job|input-file|other-field|old-attempt|consumed|unavailable/);
  page.node("#batch-uploaded-0").value = "cross-job";
  await page.node("#batch-submit").emit("click");
  assert.equal(page.requests.length, 3);
  assert.match(page.node("#error").textContent, /不属于此任务/);
});

test("two prepared files for one job bind independently to their declared output fields", async () => {
  const current = { ...task(), outputs: [parameter("document", "file"), parameter("slides", "file")], files: [
    attachment("document-id"), attachment("slides-id", { field_key: "slides", filename: "slides.pptx" }),
  ] };
  const page = await queue(current);
  await open(page, current);
  page.node("#batch-uploaded-0").value = "document-id";
  page.node("#batch-uploaded-1").value = "slides-id";
  await page.node("#batch-uploaded-0").emit("change");
  await page.node("#batch-uploaded-1").emit("change");
  assert.equal(page.node("#batch-output-0").required, false);
  assert.equal(page.node("#batch-output-1").required, false);
  const submitting = page.node("#batch-submit").emit("click");
  assert.equal(page.requests[3].url, "/api/manage/batch");
  assert.deepEqual(page.requests[3].body.output, { document: "document-id", slides: "slides-id" });
  page.requests[3].respond({ ok: true });
  await finishRefresh(page, 4);
  await submitting;
});

test("a newly selected file overrides the existing draft and submits its new opaque ID", async () => {
  const page = await queue();
  await open(page, task([attachment("previous")]));
  page.node("#batch-uploaded-0").value = "previous";
  page.node("#batch-output-0").files = [new File(["new document"], "new.pdf")];
  const submitting = page.node("#batch-submit").emit("click");
  await flush();
  assert.equal(page.requests[3].url, "/api/manage/files/upload");
  assert.equal(page.requests[3].body.job_id, "job-A");
  assert.equal(page.requests[3].body.field_key, "document");
  page.requests[3].respond({ id: "new-file" });
  await flush();
  assert.equal(page.requests[4].url, "/api/manage/batch");
  assert.deepEqual(page.requests[4].body.output, { document: "new-file" });
  page.requests[4].respond({ ok: true });
  await finishRefresh(page, 5);
  await submitting;
});

test("cancelling an in-flight upload prevents the dependent completion write", async () => {
  const page = await queue();
  await open(page, task());
  page.node("#batch-output-0").files = [new File(["document"], "new.pdf")];
  const submitting = page.node("#batch-submit").emit("click");
  await flush();
  assert.equal(page.requests[3].url, "/api/manage/files/upload");
  await page.node("#batch-cancel").emit("click");
  page.requests[3].respond({ id: "unsubmitted-draft" });
  await submitting;
  assert.equal(page.requests.length, 4);
  assert.equal(page.node("#batch-form").innerHTML, "");
});

test("a delayed fresh-file snapshot cannot render after changing queue views", async () => {
  const page = await queue();
  const opening = page.node("#complete").emit("click");
  page.set({ queueLoadId: 999 });
  page.node("#batch-form").innerHTML = "New queue view";
  page.requests[2].respond([task([attachment("old-draft")])]);
  await opening;
  assert.equal(page.node("#batch-form").innerHTML, "New queue view");
});
