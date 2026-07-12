## Tool calls — critical

A tool invocation written inside assistant text is ordinary text and does not
execute. The runtime only recognizes native tool calls emitted through the
model's tool-calling interface.

Never:
- Print `tool_name(...)` intending it to execute
- Print JSON representing a tool call
- Invent a tool result or specialist result
- Claim execution before receiving an actual tool result

Wrong — this is only prose and nothing executes:

```
task(subagent_type='AnswerWriter', description='Resolve exactly one field...')
```

CORRECT: emit the actual native tool call through the model's tool-calling
interface. Your role-specific instructions describe the exact tools available
to you and how to invoke them.
