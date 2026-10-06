# Task flow core contract

`task_flow` is optional customer/task orchestration. It is separate from a
processor profile's `workflow` (variables, secrets and execution limits).
Ordinary cards have no `card_task_flows` binding and keep the existing behavior.
Adding a graph to a product never changes cards already issued without a graph.

## Definition and issuance

`task_flow_definition.validate_definition(value, product)` returns a canonical
graph or `None`. A graph has `version: 1`, an `entry` and at most 64 `nodes`.
Each node has a unique `id`, a `kind` and optional translated `label`.

| Kind | Fields | Behavior |
| --- | --- | --- |
| `input` | `prompt`, `question`, `fields`, `start_policy`, `show_from`, `next`, optional `timeout_seconds` and `timeout_next` | `confirm` waits for Start before revealing the question and starting the clock; `automatic` starts on entry after the first node. |
| `process` | `inputs`, `outputs`, `next`, `failure_next`, `timeout_seconds`, `timeout_next` | All process nodes use the product's issued executor: queue, webhook or a catalog processor. |
| `display` | `content`, `show_from`, `next`, optional timeout | Shows selected completed results until Continue. |
| `end` | `state`, `result`, `message`, `retryable`, `needs_review` | Collects final output or ends with failure/rejection. |

References have `{node, field}`. `inputs` maps processor parameter names to
completed sources; `show_from` is an independent display map, never a prefill.
`end.result` maps final product output names to completed sources. A repeated
node resolves its most recent completed occurrence in the current job attempt.
Input and process `next` accept a target ID or ordered branches:

```json
{"cases":[{"when":{"source":{"node":"question","field":"answer"},"op":"eq","value":"yes"},"to":"process"}],"default":"failed"}
```

Operations are `eq`, `in` and `exists`. No expression/code evaluation occurs.
Entry is input or display; a fresh redemption never immediately runs a process.
The initial input or display always waits for an explicit Start, including batch redemption.
A run has at most 256 lifetime node activations, including retries and backjumps.

`task_flow.freeze_card(c, card_id, product)` is called inside card issuance after
the card is inserted. It freezes the graph, final input/output schema, delivery
policy, mode, processor ID and webhook credentials in encrypted storage.
`card_snapshot(c, card_id)` returns `{definition, product, definition_hash}` to
trusted server code. Missing binding means legacy behavior. Graphs are limited
to 100,000 UTF-8 bytes, each encrypted plaintext envelope to 200,000 bytes, and
the snapshot plus run step/dispatch ciphertext to 2 MiB per card/run. Positive
storage growth must also pass global/shop storage quota checks.

## Persistence and transaction ownership

`task_flow.init_schema(c)` only adds `card_task_flows`, `task_flow_runs` and
`task_flow_steps` plus their indexes. It changes no existing table columns.
Snapshot/step values use tenant-bound AES-GCM with separate resource AAD.
Equality receipts use a separately derived HMAC key so short sensitive values
do not leave publicly enumerable hashes.

Callers hold the existing `BEGIN IMMEDIATE` transaction. Every mutation returns
an **internal effect**:

```text
{row: sqlite.Row, flow: safe_view, terminal?: {...},
 expired?: true, duplicate?: true,
 accepted?: {node_id, flow_epoch, kind: input|output, fields, values}}
```

The sole adapter is `service.finalize_task_flow(c, effect)`. It validates/binds
`accepted` stage files and synchronizes dispatch in the same transaction. Only
an ended flow's `terminal` may finalize card/job/file/event state, exactly once.
Core process success saves intermediate encrypted output, keeps the card
reserved and leaves `jobs.result_json` empty. The terminal adapter uses the
frozen final schema, never the merchant's later edited product schema.

`terminal` is `{state, output, result_sources, message, retryable, needs_review}`.
`result_sources[final_key]` is `{node, field, flow_epoch, kind}` and is derived
only from validated completed steps. File aliases may only promote those exact
stage attachments, not a customer-supplied file ID. Internal effects, secrets,
raw graph and all historical values must never be returned from an HTTP route.

## Core API

| Function | Contract |
| --- | --- |
| `is_flow(c, row)` | True only for an issued card binding. |
| `initialize(c, row)` | Create a waiting run without starting its entry input/display timer; idempotent. |
| `preview(c, card_id)` | Safe card-specific initial preview with epoch/revision 0. |
| `view(c, row, staff=False)` | Safe current schema, selected shown results and metadata-only history. |
| `start(c, row, epoch, revision)` | Start current input or initial display once; repeats preserve the original deadline. |
| `answer(c, row, values, epoch, revision)` | Validate string values, store encrypted input and activate next node. Same-answer retry is idempotent. |
| `continue_display(c, row, epoch, revision)` | Complete a display node and activate its target. |
| `execution(c, row)` | Active unexpired process context, or `None` for legacy/nonprocess/invalid input. Never fall back to legacy execution for a flow card. |
| `claim(c, row, actor, epoch)` | Atomically claim a queued process; lease never extends its node deadline. |
| `release_claim(c, row, actor, epoch)` | Return the same process to queue without resetting epoch/deadline. |
| `process_update(c, row, update, epoch, actor=None)` | Require current attempt/epoch/state and the claimed queue actor. Progress cannot renew timers; success only stores a stage result. |
| `frozen_authority(c, row, epoch, attempt=None)` | Resolve exact historical process signing metadata for receipt replay; never returns inputs or output values. |
| `expire_due(c, now=None, limit=100)` | Bounded due-flow sweep; return effects for the adapter. |
| `reset_attempt(c, row)` | After whole customer retry increments job attempt, start a new entry while preserving monotonically increasing epoch/transition count. |
| `stop_for_outcome(c, row)` | After trusted needs-input/rejected/failed outcome, invalidate execution and clear stored private values. |
| `cancel(c, row, epoch, revision)` | Invalidate a customer-cancelled flow; started external processing requires merchant review. |
| `destroy(c, row)` | Remove all stored step values after authorized receipt destruction. |
| `stage_values(c, row, epoch, kind)` | Trusted exact-stage file validation helper; never public. |

Each activation increments `flow_epoch`, each mutation increments `revision`.
Only a whole customer retry increments `jobs.attempt`. Customer requests use
both epoch and revision; process actions use epoch, attempt and action ID plus
their authenticated actor or signed private-worker authority.

Expired mutations return `expired: true` **instead of updating then throwing**.
The HTTP adapter finalizes and commits this effect, then returns 409 or a safe
expired view outside the transaction. Updating then raising inside `db()` would
roll back expiry. A process timeout directly ends with uncertain failure,
`needs_review: true` and `retryable: false`; it does not evaluate or dispatch its
configured timeout target. The safe target ID may be recorded as audit metadata.
Input/display timeouts follow their explicit timeout target.

## Customer HTTP/UI shape

`POST /api/task-flow/start`, `/answer`, `/continue` (and `/cancel`) use
`{token, card_id?, flow_epoch, expected_revision, values?}`. `values` is a map of
strings: numbers are decimal text, booleans are `"true"`/`"false"`, and image
lists are a canonical JSON string of IDs. File ownership is checked by the
trusted stage-file adapter before commit. Responses are the ordinary safe
`job_view` object with a `task_flow` member, not the core effect.

The safe flow member has `enabled`, `version`, `definition_hash`, `attempt`,
`flow_epoch`, `revision`, `phase`, `current`, `deadline`, `server_time`, `shown`,
`history` and `actions`. Phases are `await_start`, `input`, `queued`, `processing`,
`display` and `ended`. Before Start only the prompt is visible; question and
input fields appear after Start. Shown results only contain explicitly selected
nonsensitive text/choice/boolean values. File values need an authorized stage
download endpoint and are omitted from this text view. Final delivery content
remains behind the existing reveal/destroy policy.

`public_product.task_flow_view` uses the issued card's `preview(c, card_id)`.
Each batch card starts independently; creating/submitting a batch does not start
the entry input/display timer. Aladdin's first input uses `confirm`, later inputs use
`automatic`, and `show_from` exposes the prior process answer alongside the next
question without an additional Continue click.

## Execution and private workers

`execution` returns trusted `{job_id,card_id,product_id,shop_id,attempt,
flow_epoch,node_id,action_id,mode,params,parameters,outputs,deadline,
input_expires_at,processor_id,webhook_url,webhook_secret}`. The parameters schema
is the referenced source schema renamed to each target parameter key. Public
queue/CLI DTOs must whitelist the relevant schema/input/action fields after
claiming; they never expose webhook URL or signing credentials. Normal queue
listing does not expose sensitive answers. Secret-bearing execution is never
stored in a plaintext event or job parameter column.

Private-worker HMAC v2 binds shop/product/job/attempt/epoch/node/action and
action type. A receipt retry first validates frozen historical authority,
signature, nonce and the same accepted body; a new mutation must then match the
active execution and deadline. Current shop/product/processor and webhook
credential revocation are checked even for historical receipt replay. V1 legacy
webhooks retain their existing contract.

`flow_worker.sync_dispatch(c, row)` creates/cancels an encrypted stable dispatch
for a webhook stage. `outbox_once()` performs bounded delivery outside a DB
transaction; `task_flow_once()` sweeps expiry and claims/runs script stages.
`private_worker.deliver_v2(context, *, recheck=None)` resolves only public HTTPS
destinations with a 15-second DNS limit, then calls the synchronous short-DB
`recheck` immediately before HTTP. The production closure re-resolves current
execution, epoch, shop, secret, sensitive TTL and deadline. A stable action/body
uses a fresh transport nonce on retry; callback receipts provide idempotence.

## Sensitive fields and files

Only an input `text` field may use `sensitive: true` and
`sensitive_ttl_seconds` (1..600, default 120). Such values can be referenced only
by process inputs, never by display, branching or final output. Expiry starts
when the answer is received and is capped by the input deadline. Current claimed
processing may read an unexpired value, while later processes cannot reuse it.
Completion, timeout, cancellation, retry/rejection and destruction clear raw
private step values. A persisted `input_expires_at` lets the bounded sweep erase
expired values without scanning every encrypted task. This clock follows all
remaining sensitive values across intermediate input, display and Start-waiting
nodes; those user steps keep their original deadlines. Retention cleanup also
runs when the shop/card no longer has execution authorization. A queued process whose
sensitive input expired returns to that source input in `await_start` with a new
epoch; it does not change the whole job attempt. Once external processing has
started, TTL cleanup only erases the input and preserves the original process
deadline. Expired values are not dispatched to a processor. An optional empty
sensitive value is allowed and has no secret expiry.

The stage-file adapter records exact job/attempt/node/epoch/input-or-output
ownership. Upload/download/validation require that scope; a matching field name
in another stage grants no access. `accepted` values are ephemeral trusted
adapter input and must be stripped before any API serialization.
