## Tool calls — critical

A tool call written inside assistant text is NOT a tool call and does NOT
execute.

WRONG — this is only prose/text and nothing executes:

```
{
  "type": "text",
  "text": "task(subagent_type='AnswerWriter', description='Resolve exactly one field...')"
}
```

Also wrong:

```
{
  "content": "task(subagent_type='AnswerWriter', description='Resolve exactly one field...')",
  "tool_calls": []
}
```

Both examples above are ordinary assistant text. The task does not run.

CORRECT:

Emit an actual native `task` tool call through the model's tool-calling
interface, with:
- subagent_type = "AnswerWriter"
- description = "Resolve exactly one field..."

The native call must appear as a real tool call, not inside text/content.
Never print, simulate, describe, or wrap `task(...)` inside assistant text.
