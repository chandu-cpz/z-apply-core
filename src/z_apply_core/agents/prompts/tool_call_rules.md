## Tool execution

When an action requires a tool, emit a native tool call through the model's
tool-call channel. Do not serialize the call into assistant `content`, JSON,
markdown, a `text` object, XML, or prose. Do not narrate the intended call first.

After a native tool result arrives, either make the next required native tool
call or return the requested agent result as normal text. Never fabricate a tool
result or claim an action ran without its real result.
