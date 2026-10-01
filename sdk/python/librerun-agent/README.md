# librerun-agent

The LibreRun agent SDK (blueprint S4, decision L27): one async handler
becomes a Run Contract v1 agent.

```python
from librerun_agent import serve, RunContext

async def handler(ctx: RunContext) -> dict:
    ctx.progress("running", step="gather", label="Gathering context")
    clean = await ctx.pii.redact(ctx.input["question"])
    await ctx.capabilities.audit_log("asked", {"chars": len(clean)})
    return {"answer": clean}

app = serve(handler)   # ASGI: /healthz, /v1/runs, /v1/runs/{id}/events, /v1/runs/{id}/output
```

`docs/authoring/SDK.md` in the LibreRun repository is the control-surface
reference; `docs/authoring/Container_Agents.md` is the walkthrough. The
core has no dependencies. Install it from a LibreRun checkout — it is
not published to PyPI, and a package of that name there is not the
project's: `pip install "./sdk/python/librerun-agent[uvicorn,otel]"`
adds a server and trace/log export to the chassis relay.
