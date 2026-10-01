"use client";

import { humanize } from "../lib/runPage";
import { useToast } from "../lib/toast";

/**
 * A `structured`-mode agent's final payload, rendered generically
 * (blueprint S7): one collapsible section per top-level key, and one
 * "Copy JSON" for the whole thing. The chassis knows no agent's result
 * vocabulary (B9), so a key is a heading and a value is drawn by shape.
 */
export default function StructuredOutput({ data }: { data: Record<string, unknown> }) {
  const { toast } = useToast();
  const entries = Object.entries(data);

  async function copy() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(data, null, 2));
      toast("Result copied as JSON", "success");
    } catch {
      toast("Could not copy — expand a section and select the text instead", "error");
    }
  }

  return (
    <div className="rounded border bg-white p-6" data-testid="structured-output">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold">Result</h2>
        <button
          type="button"
          onClick={copy}
          data-testid="copy-json"
          className="rounded border px-3 py-1 text-sm hover:bg-slate-50"
        >
          Copy JSON
        </button>
      </div>
      {entries.length === 0 ? (
        <p className="text-sm text-slate-500">No structured result available.</p>
      ) : (
        <div className="space-y-2">
          {entries.map(([key, value]) => (
            <details key={key} open data-testid="output-section" className="rounded border border-slate-200">
              <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-slate-700">
                {humanize(key)}
              </summary>
              <div className="border-t border-slate-200 px-3 py-2 text-sm">
                <Value value={value} />
              </div>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

function isPrimitive(v: unknown): v is string | number | boolean | null {
  return v === null || ["string", "number", "boolean"].includes(typeof v);
}

function Value({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || value === undefined) return <span className="text-slate-400">—</span>;
  if (typeof value === "string") return <p className="whitespace-pre-wrap">{value}</p>;
  if (typeof value === "number" || typeof value === "boolean") return <span>{String(value)}</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-slate-400">(empty)</span>;
    return (
      <ul className="list-disc space-y-1 pl-5">
        {value.map((item, i) => (
          <li key={i}>
            {isPrimitive(item) || depth < 2 ? <Value value={item} depth={depth + 1} /> : <Raw value={item} />}
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    if (depth >= 2) return <Raw value={value} />;
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <span className="text-slate-400">(empty)</span>;
    return (
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
        {entries.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="font-medium text-slate-600">{humanize(k)}</dt>
            <dd>
              <Value value={v} depth={depth + 1} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <Raw value={value} />;
}

function Raw({ value }: { value: unknown }) {
  return <pre className="whitespace-pre-wrap text-xs">{JSON.stringify(value, null, 2)}</pre>;
}
