/**
 * Run Contract v1 in TypeScript — the reference server (blueprint S5/S5-R).
 *
 * WHY THIS FILE EXISTS
 * The Python SDK makes a Python agent four lines long. This file is the
 * other half of the promise: any process that serves four HTTP endpoints
 * is a LibreRun agent, whatever it is written in. There is no LibreRun
 * package here — `@librerun/agent` is v1.1 (L20) — so everything the SDK
 * would have done is visible, in order, below. Read it as the spec's
 * worked example: `docs/authoring/Run_Contract_v1.md` is the contract, this is one
 * conforming implementation, and `node:http` is the whole transport.
 *
 * THE ONE THING THAT IS NOT OBVIOUS
 * The model client is built PER INVOCATION, inside `work()`:
 *
 *     createOpenAI({ baseURL, apiKey: agentKey,
 *                    headers: { "X-LibreRun-Run-Token": bearer, traceparent } })
 *
 * `apiKey` is this agent's LibreRun GATEWAY key — never a provider key —
 * and it names an agent and nothing else. The run token beside it is what
 * names the run, its tenant and its trace, so a provider is only ever
 * reached through the gateway and every call is attributable. A provider
 * built once at module scope would carry the first invocation's token into
 * every later one: on a multi-tenant deployment the gateway would then
 * resolve some other tenant's step configuration, and once that token
 * expired it would refuse the call outright (D10).
 *
 * And the model is named `librerun/answer` — a STEP id, not a model. Which
 * model answers it is the tenant admin's configuration, resolved by the
 * gateway per request (L25, D13), so retargeting the step is an edit in the
 * admin page and this container never learns it happened.
 *
 * `openai.chat(...)`, not `openai(...)`: the provider's default is the
 * Responses API and the gateway speaks OpenAI /v1/chat/completions.
 *
 * Standalone:  OPENAI_BASE_URL=... OPENAI_API_KEY=... npx tsx server.ts
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { createOpenAI } from "@ai-sdk/openai";
import { generateText, jsonSchema, stepCountIs, tool } from "ai";

const PORT = Number(process.env.PORT ?? 8090);
const AGENT_ID = "vercel-answer";
const PHASE = "answer";
const STEP = "answer";

/** How the answer was produced. Two values, kept apart on purpose. */
const FROM_MODEL = "model";
const FROM_RULE = "rule";

/** The gateway, and this agent's key for it. Both are set by
 *  agents.compose.yaml; neither is ever a provider's. */
const GATEWAY_URL = (process.env.OPENAI_BASE_URL ?? "http://gateway:8090/v1").replace(/\/$/, "");
const AGENT_KEY = process.env.OPENAI_API_KEY ?? "";

/** The tool. It reaches nothing: the `agents` network is internal, so a
 *  tool that called out would fail — which is the point of showing one
 *  that does not need to. */
const STATUS_BOARD: Record<string, string> = {
  checkout: "degraded since 09:14 UTC — elevated 502s, vendor connection pool saturated",
  payments: "operational",
  search: "operational, p95 latency 40% above baseline",
  notifications: "operational",
};

/** What went wrong, in terms an operator can act on and with no room
 *  for a provider's prose: the error's name, the HTTP status when there
 *  was one, and the gateway's own refusal code. */
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

/** The phase's work. One `generateText` with one tool, then the phase
 *  output. Nothing here knows whether the deployment has a provider
 *  account: keyless is the gateway's business. */
async function work(
  inv: Invocation,
  input: Record<string, any>,
  bearer: string,
  traceparent: string | undefined,
  deadlineSeconds: number,
): Promise<void> {
  const headers: Record<string, string> = { "X-LibreRun-Run-Token": bearer };
  if (traceparent) headers["traceparent"] = traceparent;
  const gateway = createOpenAI({ baseURL: GATEWAY_URL, apiKey: AGENT_KEY, headers });

  // The chassis fails the phase when the deadline passes; stopping
  // cleanly first is how an agent turns that into its own `failed`.
  const abort = AbortSignal.timeout(Math.max(1, deadlineSeconds) * 1000);

  emit(inv, "progress", { step_id: STEP, status: "running", label: "Asking the model", detail: null });

  // A PLAIN JSON SCHEMA, not a zod schema — and deliberately.
  //
  // `zod` is the idiomatic way to declare a tool, and converting one
  // adds a meta-schema key alongside the shape whose value is the
  // draft-07 URL. That URL sits in a position the gateway may not
  // rewrite — a schema keyword has to reach the model verbatim — so its
  // outbound walk refuses the whole request `400 pii_in_identifier`, on
  // every call, before any model sees it. Measured against the real
  // gateway; `test_two_tenants_on_the_examples.py` pins both halves, so
  // this comment cannot quietly stop being true.
  //
  // Declaring the schema directly is also the better example: this is
  // the shape that goes on the wire, and it drops a dependency.
  const serviceStatus = tool({
    description: "The current status of one of this deployment's services.",
    inputSchema: jsonSchema<{ service: string }>({
      type: "object",
      properties: { service: { type: "string", description: "the service name" } },
      required: ["service"],
      additionalProperties: false,
    }),
    execute: async ({ service }: { service: string }) => ({
      service,
      status: STATUS_BOARD[service] ?? "unknown to this agent's status board",
    }),
  });

  const service = String(input.service ?? "");
  let answer = "";
  let source = FROM_MODEL;
  let finishReason = "";
  let calls: Array<{ tool: string; input: unknown }> = [];

  try {
    const result = await generateText({
      model: gateway.chat(`librerun/${STEP}`),
      tools: { service_status: serviceStatus },
      // Two steps: call the tool, then answer with what it returned. The
      // choice is forced on the first step and withdrawn on the second so
      // the loop terminates on an answer rather than on the step cap.
      prepareStep: ({ stepNumber }: { stepNumber: number }) =>
        stepNumber === 0
          ? { toolChoice: { type: "tool" as const, toolName: "service_status" as const } }
          : { toolChoice: "none" as const },
      stopWhen: stepCountIs(2),
      abortSignal: abort,
      system:
        "You are answering an operator's question about a running system. " +
        "Check the named service's status with the tool, then answer in three sentences.",
      prompt:
        `Question: ${String(input.question ?? "")}\n` +
        (service ? `Service: ${service}\n` : ""),
    });
    answer = result.text;
    finishReason = String(result.finishReason ?? "");
    calls = result.steps.flatMap((s: any) =>
      (s.toolCalls ?? []).map((c: any) => ({ tool: String(c.toolName), input: c.input ?? c.args ?? null })),
    );
  } catch (err: unknown) {
    // THE DEADLINE IS NOT A MODEL FAILURE. The chassis fails the phase
    // when the budget passes and this agent must not pretend otherwise,
    // so an abort is re-thrown and becomes a `failed` event.
    if (abort.aborted) throw err;
    // Anything else means no model answered — the gateway is down, the
    // step is misconfigured, a provider refused. An example that died
    // there would teach an author that a model is a dependency of their
    // agent rather than an improvement on it, so this falls back to the
    // one thing the container can do alone: read its own status board.
    // What it must never do is present that as a model's answer.
    source = FROM_RULE;
    emit(inv, "log", {
      level: "warning",
      // The error's KIND, its status, and the gateway's own refusal
      // code — never a provider's message text, which may carry
      // anything and is the platform's to decide how to store. The
      // gateway's `code` is a code by contract (`unknown_step`,
      // `pii_in_identifier`, …), which is exactly what an operator
      // needs and what "AI_APICallError" alone cost one CI cycle to
      // find out.
      message: `no model answered the ${STEP} step (${describe(err)}); answering from the status board`,
    });
    calls = service ? [{ tool: "service_status", input: { service } }] : [];
    answer = service
      ? `No model answered, so this is the status board alone: ${service} is ${STATUS_BOARD[service] ?? "unknown to this agent"}.`
      : "No model answered, and no service was named to look up.";
    finishReason = "fallback";
  }

  emit(inv, "progress", { step_id: STEP, status: "completed", detail: null });

  inv.output = {
    answer,
    // Which half produced what is on screen. Not bookkeeping: a reader of
    // the run page should never have to guess whether a sentence came
    // from a model or from the table below it.
    answer_source: source,
    // Which tools were actually called, so the run page shows the work
    // rather than only its conclusion.
    tool_calls: calls,
    service: service || null,
    finish_reason: finishReason,
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

/** The invocation this request may see, or the status that refuses it.
 *  An unknown id is 404; a known one with a missing or foreign bearer is
 *  401 — the contract's binding rule, and what the container battery
 *  probes from four directions. */
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
  const deadline = Number(body.deadline_seconds ?? 600);
  void work(inv, body.input ?? {}, bearer, traceparent, deadline).catch((err: unknown) => {
    // Operator-facing by contract, and redacted by the chassis before it
    // is logged or stored: the message, never a stack.
    inv.status = "failed";
    inv.error = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
    emit(inv, "failed", { error: inv.error });
  });

  // `run_id` beside `invocation_id` until v1.1: the chassis reads the
  // first and accepts the second for one release.
  send(res, 201, { invocation_id: id, run_id: id, agent_id: AGENT_ID });
}

function handleEvents(req: IncomingMessage, res: ServerResponse, id: string): void {
  const inv = authorize(req, id);
  if (typeof inv === "number") return send(res, inv, { error: inv === 404 ? "unknown invocation" : "unauthorized" });

  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "close",
  });
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
  // The only line this process prints. `logging: driver: none` means the
  // daemon persists nothing anyway; an agent's operator-facing lines
  // belong on the contract's `log` event.
  console.log(`${AGENT_ID} serving Run Contract v1 on :${PORT}`);
});
