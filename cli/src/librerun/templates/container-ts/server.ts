/**
 * __AGENT_NAME__ — Run Contract v1 in TypeScript.
 *
 * Scaffolded by `librerun init __AGENT_ID__ --template container-ts`, from
 * the reference server (backend/agents/_examples/vercel_ai_answer_ts/server.ts).
 * Any process that serves four HTTP endpoints is a LibreRun agent, whatever
 * it is written in; there is no LibreRun package here (`@librerun/agent` is
 * v1.1), so everything the Python SDK would do is visible below, in order.
 * `docs/authoring/Run_Contract_v1.md` is the contract; `node:http` is the transport.
 *
 * THE ONE THING THAT IS NOT OBVIOUS
 * The model client is built PER INVOCATION, inside `work()`:
 *
 *     createOpenAI({ baseURL, apiKey: agentKey,
 *                    headers: { "X-LibreRun-Run-Token": bearer, traceparent } })
 *
 * `apiKey` is this agent's LibreRun GATEWAY key — never a provider key —
 * and it names an agent and nothing else. The run token beside it names
 * the run, its tenant and its trace, so every model call is attributable
 * (D10). A provider built once at module scope would carry the first
 * invocation's token into every later one. And the model is named
 * `librerun/answer` — a STEP id, not a model: which model answers is the
 * tenant admin's configuration, resolved by the gateway per request (L25,
 * D13), so retargeting it is an edit in the admin page.
 *
 * Standalone:  OPENAI_BASE_URL=... OPENAI_API_KEY=... npx tsx server.ts
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { createOpenAI } from "@ai-sdk/openai";
import { generateText } from "ai";

const PORT = Number(process.env.PORT ?? 8090);
const AGENT_ID = "__AGENT_ID__";
const PHASE = "answer";
const STEP = "answer"; // declared under llm.steps in agent.yaml

/** What `answer_source` may say. Keyless, the gateway answers from THIS
 *  agent's own fixture as an ordinary completion, so "a call succeeded" and
 *  "a model decided" are different questions; the gateway's `librerun`
 *  envelope names the provider, and the label is read from it. */
const SOURCE_RULES = "rules";
const SOURCE_STUB = "stub-fixture";
const SOURCE_MODEL = "model";
const SOURCE_UNKNOWN = "unattributed";

/** The gateway, and this agent's key for it. Both are set by the compose
 *  service; neither is ever a provider's. */
const GATEWAY_URL = (process.env.OPENAI_BASE_URL ?? "http://gateway:8090/v1").replace(/\/$/, "");
const AGENT_KEY = process.env.OPENAI_API_KEY ?? "";

/** The two LibreRun extensions travel in the request and reply BODIES,
 *  which the AI SDK neither sends nor surfaces: `librerun.stub_reply`, this
 *  agent's own keyless fixture (honoured by the gateway only when the
 *  resolved provider is the stub), and the reply's `librerun.provider`,
 *  which says who actually answered. So the client's fetch is wrapped, per
 *  invocation: the fixture is added to the outgoing chat body and the
 *  provider is read off a clone of the reply. A few lines, and the only
 *  place this file touches the wire directly. */
function gatewayFetch(fixture: string, seen: { provider?: string }): typeof fetch {
  return async (input, init) => {
    let request = init;
    if (init?.body && typeof init.body === "string" && String(input).endsWith("/chat/completions")) {
      try {
        const body = JSON.parse(init.body) as Record<string, unknown>;
        request = { ...init, body: JSON.stringify({ ...body, librerun: { stub_reply: fixture } }) };
      } catch {
        // Not JSON; send it as it is.
      }
    }
    const res = await fetch(input, request);
    try {
      const body = (await res.clone().json()) as { librerun?: { provider?: unknown } };
      if (body?.librerun?.provider != null) seen.provider = String(body.librerun.provider);
    } catch {
      // Not the gateway's JSON envelope (an error page, a stream): nothing to read.
    }
    return res;
  };
}

/** The deterministic answer: the fallback when no model answers, and the
 *  keyless fixture handed to the gateway — one function, so the two can
 *  never drift apart. */
function byTheRules(question: string, context: string): string {
  if (context.trim()) return `Start from what changed: ${context.trim().slice(0, 200)}`;
  return `Restate the question and gather one concrete example: ${question.trim().slice(0, 200)}`;
}

/** What went wrong, in terms an operator can act on and with no room for
 *  a provider's prose: the error's name, the HTTP status when there was
 *  one, and the gateway's own refusal code. */
function describe(err: unknown): string {
  const parts: string[] = [err instanceof Error ? err.name : "error"];
  const status = (err as { statusCode?: unknown })?.statusCode;
  if (typeof status === "number") parts.push(`status ${status}`);
  const raw = (err as { responseBody?: unknown })?.responseBody;
  if (typeof raw === "string") {
    try {
      const code = (JSON.parse(raw) as { error?: { code?: unknown } })?.error?.code;
      if (typeof code === "string") parts.push(code);
    } catch {
      // Not the gateway's JSON envelope; the name and status say enough.
    }
  }
  return parts.join(", ");
}

type Invocation = {
  bearer: string;
  status: "running" | "completed" | "failed";
  events: Array<{ event: string; data: unknown }>;
  output?: Record<string, unknown>;
  error?: string;
  wake: Array<() => void>;
};

const invocations = new Map<string, Invocation>();

function emit(inv: Invocation, event: string, data: unknown): void {
  inv.events.push({ event, data });
  for (const wake of inv.wake.splice(0)) wake();
}

/** The intake PII pipeline on demand (the `pii` grant): the run-scoped MCP
 *  `redact` tool at run.mcp.url, with the invocation's bearer. Anything
 *  this agent fetched itself never passed through intake, so it goes
 *  through here before a model or the output sees it. */
async function redact(mcpUrl: string | undefined, bearer: string, text: string): Promise<string> {
  // The chassis always advertises run.mcp.url; the container battery,
  // driving this server on its own, does not — and the caller then leaves
  // the fetched text out rather than using it unredacted.
  if (!mcpUrl) throw new Error("this invocation advertised no run.mcp.url");
  const res = await fetch(mcpUrl, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${bearer}` },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "redact", arguments: { text } },
    }),
  });
  const body = (await res.json()) as {
    result?: { content?: Array<{ type?: string; text?: string }> };
    error?: { code?: number; message?: string };
  };
  if (body.error) throw new Error(body.error.message ?? "redact refused");
  // One text content block carrying JSON — `{"text": "<redacted>"}` — the
  // shape every tool of the run-scoped MCP server answers with.
  const block = body.result?.content?.find((c) => c.type === "text")?.text;
  const parsed = typeof block === "string" ? (JSON.parse(block) as { text?: unknown }) : {};
  return typeof parsed.text === "string" ? parsed.text : text;
}

/** One of this agent's tool secrets (K8b) — a key of its own for a service
 *  it calls: this tenant's value, else every tenant's default, from the
 *  run-scoped MCP `secret_get` tool, with the invocation's bearer — the
 *  same raw fetch as `redact`. The name must be declared in agent.yaml's
 *  `secrets:`, and this template declares none, so nothing calls this
 *  until you do. `null` is -32006 secret_not_set, a declared name nobody
 *  has set: read it as "no key" and carry on without the service. Any
 *  other refusal throws an Error naming its code — -32005
 *  secret_not_declared is a name agent.yaml does not declare, a typo
 *  rather than a state. The value is this invocation's alone: never log
 *  it, emit it or put it in the output, which the chassis persists. */
async function secretGet(mcpUrl: string | undefined, bearer: string, name: string): Promise<string | null> {
  if (!mcpUrl) throw new Error("this invocation advertised no run.mcp.url");
  const res = await fetch(mcpUrl, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${bearer}` },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "secret_get", arguments: { name } },
    }),
  });
  const body = (await res.json()) as {
    result?: { content?: Array<{ type?: string; text?: string }> };
    error?: { code?: number; message?: string };
  };
  if (body.error?.code === -32006) return null;
  if (body.error) {
    const code = body.error.code === -32005 ? "-32005 secret_not_declared" : String(body.error.code ?? "no code");
    throw new Error(`secret_get refused ${JSON.stringify(name)}: ${code}`);
  }
  const block = body.result?.content?.find((c) => c.type === "text")?.text;
  const parsed = typeof block === "string" ? (JSON.parse(block) as { value?: unknown }) : {};
  if (typeof parsed.value !== "string") throw new Error("secret_get answered without a value");
  return parsed.value;
}

/** The phase's work: one generateText for the step, then the phase output.
 *  Nothing here knows whether the deployment has a provider account:
 *  keyless is the gateway's business. */
async function work(
  inv: Invocation,
  input: Record<string, any>,
  bearer: string,
  traceparent: string | undefined,
  mcpUrl: string | undefined,
  deadlineSeconds: number,
): Promise<void> {
  const headers: Record<string, string> = { "X-LibreRun-Run-Token": bearer };
  if (traceparent) headers["traceparent"] = traceparent;

  // The chassis fails the phase when the deadline passes; stopping cleanly
  // first is how an agent turns that into its own `failed`.
  const abort = AbortSignal.timeout(Math.max(1, deadlineSeconds) * 1000);

  emit(inv, "progress", { step_id: STEP, status: "running", label: "Answering", detail: null });

  const question = String(input.question ?? "");
  const context = String(input.context ?? "");
  const rules = byTheRules(question, context);

  // Something this agent "fetched" after intake, redacted before use.
  let fetched = `Context note for ${question.slice(0, 80)}`;
  try {
    fetched = await redact(mcpUrl, bearer, fetched);
  } catch (err: unknown) {
    emit(inv, "log", { level: "warning", message: `redact unavailable (${describe(err)}); the fetched note is not used` });
    fetched = "";
  }

  // Built PER INVOCATION — see the header — with this invocation's run
  // token in its headers and its own fixture and envelope capture.
  const seen: { provider?: string } = {};
  const gateway = createOpenAI({ baseURL: GATEWAY_URL, apiKey: AGENT_KEY, headers, fetch: gatewayFetch(rules, seen) });

  let answer = rules;
  let source = SOURCE_RULES;
  try {
    const result = await generateText({
      // `openai.chat(...)`, not `openai(...)`: the provider's default is the
      // Responses API and the gateway speaks OpenAI /v1/chat/completions.
      model: gateway.chat(`librerun/${STEP}`),
      abortSignal: abort,
      system: "Answer the operator's question in two sentences, using the context.",
      prompt: `Question: ${question}\nContext: ${context}\n${fetched}`,
    });
    if (result.text.trim()) {
      answer = result.text;
      source = seen.provider == null ? SOURCE_UNKNOWN : seen.provider.toLowerCase() === "stub" ? SOURCE_STUB : SOURCE_MODEL;
    }
  } catch (err: unknown) {
    // THE DEADLINE IS NOT A MODEL FAILURE: an abort is re-thrown and becomes
    // a `failed` event. Anything else means no model answered, and the
    // rule stands — labelled as the rule, never as a model's answer.
    if (abort.aborted) throw err;
    emit(inv, "log", {
      level: "warning",
      message: `no model answered the ${STEP} step (${describe(err)}); answering by rule`,
    });
  }

  emit(inv, "progress", { step_id: STEP, status: "completed", detail: null });

  inv.output = {
    question,
    answer,
    answer_source: source,
    framework: "vercel-ai-sdk",
  };
  inv.status = "completed";
  emit(inv, "completed", { output: inv.output });
}

// ---------------------------------------------------------------------------
// Run Contract v1: four endpoints, and the bearer bound to the invocation.
// ---------------------------------------------------------------------------

function send(res: ServerResponse, status: number, body: unknown): void {
  const raw = Buffer.from(JSON.stringify(body));
  res.writeHead(status, { "content-type": "application/json", "content-length": raw.length });
  res.end(raw);
}

function bearerOf(req: IncomingMessage): string | null {
  const header = req.headers.authorization ?? "";
  return header.toLowerCase().startsWith("bearer ") ? header.slice(7).trim() || null : null;
}

/** The invocation this request may see, or the status that refuses it: an
 *  unknown id is 404; a known one with a missing or foreign bearer is 401
 *  — the contract's binding rule, which the container battery probes. */
function authorize(req: IncomingMessage, id: string): Invocation | number {
  const inv = invocations.get(id);
  if (!inv) return 404;
  const bearer = bearerOf(req);
  if (!bearer || bearer !== inv.bearer) return 401;
  return inv;
}

async function readJson(req: IncomingMessage): Promise<any> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {};
}

async function handlePost(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const body = await readJson(req);
  if (body.contract !== "v1") return send(res, 400, { error: `unsupported contract ${body.contract}` });
  if (body.phase !== PHASE) return send(res, 400, { error: `unknown phase ${body.phase}` });
  const bearer = bearerOf(req);
  if (!bearer) return send(res, 401, { error: "a bearer token is required" });

  const id = randomUUID();
  const inv: Invocation = { bearer, status: "running", events: [], wake: [] };
  invocations.set(id, inv);

  const traceparent = typeof req.headers.traceparent === "string" ? req.headers.traceparent : undefined;
  const mcpUrl = typeof body?.run?.mcp?.url === "string" ? body.run.mcp.url : undefined;
  const deadline = Number(body.deadline_seconds ?? 600);
  void work(inv, body.input ?? {}, bearer, traceparent, mcpUrl, deadline).catch((err: unknown) => {
    inv.status = "failed";
    inv.error = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
    emit(inv, "failed", { error: inv.error });
  });

  // `run_id` beside `invocation_id` until v1.1: the chassis reads the first
  // and accepts the second for one release.
  send(res, 201, { invocation_id: id, run_id: id, agent_id: AGENT_ID });
}

function handleEvents(req: IncomingMessage, res: ServerResponse, id: string): void {
  const inv = authorize(req, id);
  if (typeof inv === "number") return send(res, inv, { error: inv === 404 ? "unknown invocation" : "unauthorized" });

  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "close" });
  let sent = 0;
  const pump = (): void => {
    while (sent < inv.events.length) {
      const { event, data } = inv.events[sent++];
      res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    }
    if (inv.status === "running") inv.wake.push(pump);
    else res.end();
  };
  pump();
}

function handleOutput(req: IncomingMessage, res: ServerResponse, id: string): void {
  const inv = authorize(req, id);
  if (typeof inv === "number") return send(res, inv, { error: inv === 404 ? "unknown invocation" : "unauthorized" });
  if (inv.status === "failed") return send(res, 409, { error: inv.error ?? "the invocation failed" });
  if (inv.status !== "completed") return send(res, 404, { error: "the invocation has not completed" });
  send(res, 200, { output: inv.output });
}

const server = createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://agent");
  const path = url.pathname;
  const run = /^\/v1\/runs\/([^/]+)\/(events|output)$/.exec(path);
  try {
    if (req.method === "GET" && path === "/healthz") return send(res, 200, { status: "ok", agent: AGENT_ID });
    if (req.method === "POST" && path === "/v1/runs") {
      handlePost(req, res).catch((err: unknown) => send(res, 400, { error: String(err) }));
      return;
    }
    if (req.method === "GET" && run) {
      const id = decodeURIComponent(run[1]);
      return run[2] === "events" ? handleEvents(req, res, id) : handleOutput(req, res, id);
    }
    send(res, 404, { error: "not found" });
  } catch (err: unknown) {
    send(res, 500, { error: String(err) });
  }
});

server.listen(PORT, "0.0.0.0", () => {
  // The only line this process prints: `logging: driver: none` persists
  // nothing anyway; operator-facing lines belong on the `log` event.
  console.log(`${AGENT_ID} serving Run Contract v1 on :${PORT}`);
});
