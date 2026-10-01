"""The LibreRun gateway: the one process that holds a provider credential.

Blueprint S4a (L23, L24, L25). Every model call an agent makes — in
process or in a container, on the SDK or through a framework that only
knows ``OPENAI_BASE_URL`` — arrives here as an OpenAI-compatible
request. The gateway authorizes it against the agent's manifest
snapshot, resolves the step's provider and model from the tenant's admin
configuration, redacts what the model will read, calls the provider
through LiteLLM, and writes the one LLM span that carries the model,
the token counts and the cost.

It runs as its own service on purpose: egress code inside the backend
would keep provider credentials in the one process every
``python-package`` agent shares.
"""
