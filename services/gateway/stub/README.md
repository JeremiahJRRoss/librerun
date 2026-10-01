# Stub reply scripts

Keyless mode (`LIBRERUN_STUB_LLM=true`) answers every model call from
the gateway's `stub` provider. By default the reply is synthesised —
from the request's own `response_format` schema when it has one, so an
agent that parses structured output gets something its normalizer
accepts without any agent-specific fixture living in the platform.

A test that needs a *particular* reply names a script here with the
`X-LibreRun-Scenario` request header: `X-LibreRun-Scenario: pii-in-reply`
serves `pii-in-reply.json`. The name is one path segment and is refused
rather than resolved if it is anything else, so the header cannot read a
file outside this directory.

A script is a JSON object:

```json
{
  "content": "what the assistant says",
  "tool_calls": [
    {"id": "call_1", "function": {"name": "a_tool", "arguments": {"any": "json"}}}
  ]
}
```

`arguments` may be an object (it is serialised for you) or an already
serialised string.
