# Task-flow examples

These definitions use `task_flow` version 1. Each directory contains:

- `flow.json`: the graph to assign to a product's `task_flow` field.
- `product.json`: the companion product configuration used for semantic validation.

The examples use queue processing (`mode: "manual"`). An authorized person or AI
must claim and complete each process node; the examples do not call an AI model,
produce a document, or contact another service by themselves.

| Example | Scenario | Final output |
| --- | --- | --- |
| `aladdin` | Three timed questions, showing each answer before the next question | Three text answers |
| `file-transform` | Submit a file and instructions, receive a newly uploaded output file | File and delivery notes |
| `choice-and-rejection` | Customer choice and a processor decision route to success or rejection | Text when accepted; no delivery on rejection |

From the repository root, validate all examples without contacting a server or
issuing cards:

```sh
uv run python examples/workflows/validate_examples.py
```

Validation checks structure, references and compatibility with the companion
product. It does not prove a processor will generate a correct answer or file.
Read [workflow development](../../docs/workflow-development.md) before adapting
an example, choosing an executor or handling retries and external effects.
