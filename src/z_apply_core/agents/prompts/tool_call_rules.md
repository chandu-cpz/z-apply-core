## Native tool calls

When a tool is required, emit it through the native tool-call channel. Never put
a would-be tool call in assistant text, JSON, markdown, XML, or a `text` object.
Never invent a tool result. Read each real tool result before choosing the next
action.
