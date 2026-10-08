# External worker SDK example

`welcome.py` shows the reviewed external worker stdin/stdout protocol. It is not
a selectable website processor and does not start a queue consumer.

Run it from the repository root with a task envelope on standard input:

```sh
uv run python examples/sdk/welcome.py < task.json
```

The envelope must follow the [SDK task contract](../../docs/python-sdk.md), with a
`name` input parameter. This example writes a progress update and a welcome
message. Use the [worker instructions](../../docs/private-worker.md) for a live
queue and the [processor guide](../../docs/processor-contributing.md) for reviewed
built-in processors.
