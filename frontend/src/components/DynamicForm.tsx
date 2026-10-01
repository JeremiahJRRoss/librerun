"use client";

import { useMemo, useState } from "react";
import type {
  AgentIntakeStep,
  JsonSchema,
  JsonSchemaField,
  PiiRedactionPreview,
} from "../types";

// Schema-driven form for any agent (blueprint B8). Two modes:
//
// - Single page (no ``steps``): every schema property in one form — the
//   pre-B8 behaviour.
// - Stepped wizard (``steps`` from the agent manifest's ui.intake.steps):
//   chip header, per-step fields with Back/Next gating, informational
//   steps (no fields), an automatic "Details" step for schema properties
//   no step claimed, and a chassis-provided final Review step.
//
// ``x-pii`` string fields render a redaction-preview file upload when the
// caller provides ``piiPreview`` (wired to POST /files/redact-preview).

interface Props {
  schema: JsonSchema;
  initial?: Record<string, unknown>;
  submitLabel?: string;
  onSubmit: (values: Record<string, unknown>) => void;
  // Open on the chassis Review step (blueprint S7): a sample loaded from
  // an agent card has every field filled, so the reader's next click is
  // Submit — Back still walks the steps.
  startAtReview?: boolean;
  submitting?: boolean;
  steps?: AgentIntakeStep[];
  piiPreview?: (file: File, kind: string) => Promise<PiiRedactionPreview | null>;
}

type FieldValue = string | number | boolean | string[] | Record<string, unknown> | null;

function defaultFor(field: JsonSchemaField): FieldValue {
  switch (field.type) {
    case "boolean":
      return false;
    case "integer":
    case "number":
      return "" as unknown as number; // empty string keeps the input controlled
    case "array":
      return [];
    case "object":
      return {};
    default:
      return "";
  }
}

function buildInitial(
  schema: JsonSchema,
  initial: Record<string, unknown> | undefined,
): Record<string, FieldValue> {
  const out: Record<string, FieldValue> = {};
  for (const [key, field] of Object.entries(schema.properties)) {
    if (initial && key in initial) {
      out[key] = initial[key] as FieldValue;
    } else {
      out[key] = defaultFor(field);
    }
  }
  return out;
}

interface FieldProps {
  name: string;
  field: JsonSchemaField;
  value: FieldValue;
  required: boolean;
  onChange: (next: FieldValue) => void;
  piiPreview?: (file: File, kind: string) => Promise<PiiRedactionPreview | null>;
}

function CharCount({ field, value }: { field: JsonSchemaField; value: FieldValue }) {
  if (!field.minLength && !field.maxLength) return null;
  const len = typeof value === "string" ? value.length : 0;
  return (
    <span className="mt-1 block text-xs text-slate-500">
      {len} chars
      {field.minLength ? ` (min ${field.minLength}` : ""}
      {field.minLength && field.maxLength
        ? `, max ${field.maxLength})`
        : field.minLength
        ? ")"
        : field.maxLength
        ? ` (max ${field.maxLength})`
        : ""}
    </span>
  );
}

function FieldInput({ name, field, value, required, onChange, piiPreview }: FieldProps) {
  const label = (
    <span className="block text-sm font-medium">
      {field.title ?? name}
      {required && <span className="text-red-600"> *</span>}
    </span>
  );
  const description = field.description ? (
    <span className="block text-xs text-slate-500">{field.description}</span>
  ) : null;

  // Enum string → dropdown
  if (field.type === "string" && Array.isArray(field.enum) && field.enum.length > 0) {
    return (
      <label className="block">
        {label}
        {description}
        <select
          className="mt-1 w-full rounded border px-2 py-1"
          value={(value as string) ?? ""}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">—</option>
          {field.enum.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
      </label>
    );
  }

  // PII-marked string → textarea + redaction-preview upload (blueprint B8)
  if (field.type === "string" && field["x-pii"] && piiPreview) {
    return (
      <div className="block">
        {label}
        {description}
        <input
          type="file"
          className="mt-1 block text-sm"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            if (!f) return;
            const preview = await piiPreview(f, field["x-upload-kind"] ?? "log");
            if (preview) onChange(preview.redacted_content);
          }}
        />
        <textarea
          rows={8}
          className="mt-2 w-full rounded border px-2 py-1 font-mono text-xs"
          placeholder="…or paste content here"
          value={(value as string) ?? ""}
          onChange={(e) => onChange(e.target.value)}
        />
        <CharCount field={field} value={value} />
      </div>
    );
  }

  // String + textarea hint
  if (field.type === "string" && field["x-ui-widget"] === "textarea") {
    return (
      <label className="block">
        {label}
        {description}
        <textarea
          rows={5}
          className="mt-1 w-full rounded border px-2 py-1"
          value={(value as string) ?? ""}
          onChange={(e) => onChange(e.target.value)}
        />
        <CharCount field={field} value={value} />
      </label>
    );
  }

  if (field.type === "string" || field.type === undefined) {
    return (
      <label className="block">
        {label}
        {description}
        <input
          type="text"
          className="mt-1 w-full rounded border px-2 py-1"
          value={(value as string) ?? ""}
          onChange={(e) => onChange(e.target.value)}
        />
      </label>
    );
  }

  if (field.type === "integer" || field.type === "number") {
    return (
      <label className="block">
        {label}
        {description}
        <input
          type="number"
          step={field.type === "integer" ? 1 : "any"}
          className="mt-1 w-full rounded border px-2 py-1"
          value={value === "" || value === null || value === undefined ? "" : (value as number)}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              onChange("" as unknown as number);
              return;
            }
            const n = field.type === "integer" ? parseInt(raw, 10) : parseFloat(raw);
            onChange(Number.isFinite(n) ? n : ("" as unknown as number));
          }}
        />
      </label>
    );
  }

  if (field.type === "boolean") {
    return (
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          className="h-4 w-4"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="text-sm">
          {field.title ?? name}
          {required && <span className="text-red-600"> *</span>}
          {field.description && (
            <span className="block text-xs text-slate-500">{field.description}</span>
          )}
        </span>
      </label>
    );
  }

  // Array of strings → tag input
  if (
    field.type === "array" &&
    (!field.items || field.items.type === "string" || field.items.type === undefined)
  ) {
    return (
      <TagInput
        labelNode={label}
        descriptionNode={description}
        value={(value as string[]) ?? []}
        onChange={(next) => onChange(next)}
      />
    );
  }

  // Nested object → fieldset
  if (field.type === "object" && field.properties) {
    const nested = (value as Record<string, unknown>) ?? {};
    return (
      <fieldset className="rounded border p-3">
        <legend className="px-2 text-sm font-medium">
          {field.title ?? name}
          {required && <span className="text-red-600"> *</span>}
        </legend>
        {description}
        <div className="mt-2 space-y-3">
          {Object.entries(field.properties).map(([childKey, childField]) => {
            const childRequired = (field.required ?? []).includes(childKey);
            return (
              <FieldInput
                key={childKey}
                name={childKey}
                field={childField}
                value={(nested[childKey] ?? defaultFor(childField)) as FieldValue}
                required={childRequired}
                onChange={(next) => onChange({ ...nested, [childKey]: next })}
                piiPreview={piiPreview}
              />
            );
          })}
        </div>
      </fieldset>
    );
  }

  // Fallback: JSON textarea so unknown shapes are still editable.
  return (
    <label className="block">
      {label}
      {description}
      <textarea
        rows={3}
        className="mt-1 w-full rounded border px-2 py-1 font-mono text-xs"
        value={value === undefined || value === null ? "" : JSON.stringify(value, null, 2)}
        onChange={(e) => {
          try {
            onChange(JSON.parse(e.target.value));
          } catch {
            // Ignore; keep last valid value.
          }
        }}
      />
    </label>
  );
}

interface TagInputProps {
  labelNode: React.ReactNode;
  descriptionNode: React.ReactNode;
  value: string[];
  onChange: (next: string[]) => void;
}

function TagInput({ labelNode, descriptionNode, value, onChange }: TagInputProps) {
  const [draft, setDraft] = useState("");
  function commit() {
    const trimmed = draft.trim();
    if (!trimmed) return;
    onChange([...value, trimmed]);
    setDraft("");
  }
  return (
    <div>
      {labelNode}
      {descriptionNode}
      <div className="mt-1 flex flex-wrap items-center gap-1 rounded border bg-white px-2 py-1">
        {value.map((tag, i) => (
          <span
            key={`${tag}-${i}`}
            className="flex items-center gap-1 rounded-full bg-slate-200 px-2 py-0.5 text-xs"
          >
            {tag}
            <button
              type="button"
              className="text-slate-500 hover:text-slate-800"
              onClick={() => onChange(value.filter((_, idx) => idx !== i))}
              aria-label={`Remove ${tag}`}
            >
              ×
            </button>
          </span>
        ))}
        <input
          className="min-w-[8rem] flex-1 border-none px-1 py-0.5 text-sm focus:outline-none"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              commit();
            } else if (e.key === "Backspace" && !draft && value.length) {
              onChange(value.slice(0, -1));
            }
          }}
          onBlur={commit}
          placeholder="Type and press Enter…"
        />
      </div>
    </div>
  );
}

// Render one value on the Review step, tolerating every field shape.
function ReviewValue({ value }: { value: FieldValue }) {
  if (value === null || value === undefined || value === "") {
    return <span className="text-slate-400">—</span>;
  }
  if (typeof value === "string") {
    const text = value.length > 400 ? `${value.slice(0, 400)}…` : value;
    return <span className="whitespace-pre-wrap break-words">{text}</span>;
  }
  if (Array.isArray(value)) {
    return <span>{value.join(", ") || "—"}</span>;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value).filter(
      ([, v]) => v !== "" && v !== null && v !== undefined,
    );
    if (entries.length === 0) return <span className="text-slate-400">—</span>;
    return (
      <span>
        {entries.map(([k, v]) => (
          <span key={k} className="block">
            <span className="text-slate-500">{k}:</span> {String(v)}
          </span>
        ))}
      </span>
    );
  }
  return <span>{String(value)}</span>;
}

export default function DynamicForm({
  schema,
  initial,
  submitLabel = "Submit",
  onSubmit,
  submitting,
  steps,
  piiPreview,
  startAtReview = false,
}: Props) {
  const required = useMemo(() => new Set(schema.required ?? []), [schema]);
  const [values, setValues] = useState<Record<string, FieldValue>>(() =>
    buildInitial(schema, initial),
  );
  const [errors, setErrors] = useState<string[]>([]);

  // Effective wizard steps: manifest steps filtered to real schema fields,
  // plus an automatic "Details" step for unclaimed properties, plus the
  // chassis Review step. null = single-page mode.
  const contentSteps = useMemo<AgentIntakeStep[] | null>(() => {
    if (!steps || steps.length === 0) return null;
    const known = new Set(Object.keys(schema.properties));
    const claimed = new Set<string>();
    const list = steps.map((s) => {
      const fields = s.fields.filter((f) => known.has(f));
      fields.forEach((f) => claimed.add(f));
      return { ...s, fields };
    });
    const unclaimed = [...known].filter((k) => !claimed.has(k));
    if (unclaimed.length > 0) {
      list.push({ title: "Details", description: "", fields: unclaimed });
    }
    return list;
  }, [steps, schema]);

  const [stepIndex, setStepIndex] = useState(() =>
    startAtReview && contentSteps ? contentSteps.length : 0,
  );

  const stepTitles = useMemo(
    () => (contentSteps ? [...contentSteps.map((s) => s.title), "Review"] : []),
    [contentSteps],
  );
  const onReview = contentSteps !== null && stepIndex === contentSteps.length;

  function setField(key: string, next: FieldValue) {
    setValues((prev) => ({ ...prev, [key]: next }));
  }

  function fieldTitle(key: string): string {
    return schema.properties[key]?.title ?? key;
  }

  // min/maxLength issues for one string value — the client-side twin of
  // the server's schema bounds, so the wizard gates exactly what the API
  // would 422 on.
  function lengthIssues(title: string, field: JsonSchemaField | undefined, v: unknown): string[] {
    const issues: string[] = [];
    if (typeof v !== "string" || !field) return issues;
    if (v.length > 0 && field.minLength && v.trim().length < field.minLength) {
      issues.push(`${title} needs at least ${field.minLength} characters`);
    }
    if (field.maxLength && v.length > field.maxLength) {
      issues.push(`${title} must be at most ${field.maxLength} characters`);
    }
    return issues;
  }

  // Client-side twin of the server's schema validation, per field:
  // required presence and string bounds, nested object properties
  // included (the form renders one object level deep).
  function fieldIssues(key: string): string[] {
    const field = schema.properties[key];
    const v = values[key];
    const issues: string[] = [];
    const empty =
      v === null ||
      v === undefined ||
      (typeof v === "string" && v.trim().length === 0) ||
      (Array.isArray(v) && v.length === 0) ||
      (typeof v === "object" && !Array.isArray(v) && Object.keys(v as object).length === 0);
    if (required.has(key) && empty) {
      issues.push(`${fieldTitle(key)} is required`);
      return issues;
    }
    if (field?.type === "object" && field.properties && !empty) {
      const nested = (v as Record<string, unknown>) ?? {};
      for (const [childKey, childField] of Object.entries(field.properties)) {
        const cv = nested[childKey];
        const childTitle = childField.title ?? childKey;
        if (
          (field.required ?? []).includes(childKey) &&
          (cv === null || cv === undefined || (typeof cv === "string" && cv.trim() === ""))
        ) {
          issues.push(`${fieldTitle(key)}: ${childTitle} is required`);
          continue;
        }
        issues.push(
          ...lengthIssues(`${fieldTitle(key)}: ${childTitle}`, childField, cv),
        );
      }
    }
    issues.push(...lengthIssues(fieldTitle(key), field, v));
    if (required.has(key) && field?.minLength && typeof v === "string" && v.trim().length === 0) {
      issues.push(`${fieldTitle(key)} needs at least ${field.minLength} characters`);
    }
    return issues;
  }

  function validate(keys: string[]): boolean {
    const found = keys.flatMap(fieldIssues);
    setErrors(found);
    return found.length === 0;
  }

  function buildPayload(): Record<string, unknown> {
    // Strip empty optional values so the API receives a clean payload.
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(values)) {
      if (v === "" || v === null || v === undefined) {
        if (required.has(k)) out[k] = v;
        continue;
      }
      if (Array.isArray(v) && v.length === 0 && !required.has(k)) continue;
      if (typeof v === "object" && !Array.isArray(v)) {
        // Drop empty-string members of nested objects (e.g. vendor.product).
        const cleaned = Object.fromEntries(
          Object.entries(v as Record<string, unknown>).filter(
            ([, cv]) => cv !== "" && cv !== null && cv !== undefined,
          ),
        );
        if (Object.keys(cleaned).length === 0 && !required.has(k)) continue;
        out[k] = cleaned;
        continue;
      }
      out[k] = v;
    }
    return out;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate(Object.keys(schema.properties))) return;
    onSubmit(buildPayload());
  }

  const errorBox =
    errors.length > 0 ? (
      <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800">
        {errors.map((err) => (
          <div key={err}>{err}</div>
        ))}
      </div>
    ) : null;

  // ---- single-page mode (no steps declared) -------------------------------
  if (!contentSteps) {
    return (
      <form onSubmit={handleSubmit} className="space-y-4">
        {Object.entries(schema.properties).map(([key, field]) => (
          <FieldInput
            key={key}
            name={key}
            field={field}
            value={values[key]}
            required={required.has(key)}
            onChange={(next) => setField(key, next)}
            piiPreview={piiPreview}
          />
        ))}
        {errorBox}
        <button
          type="submit"
          disabled={submitting}
          className="rounded bg-blue-600 px-6 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {submitting ? "Submitting…" : submitLabel}
        </button>
      </form>
    );
  }

  // ---- stepped wizard mode -------------------------------------------------
  const current = onReview ? null : contentSteps[stepIndex];

  function next() {
    if (current && !validate(current.fields)) return;
    setErrors([]);
    setStepIndex((i) => Math.min(i + 1, contentSteps!.length));
  }

  function back() {
    setErrors([]);
    setStepIndex((i) => Math.max(i - 1, 0));
  }

  return (
    <form onSubmit={handleSubmit}>
      <div className="mb-6 flex gap-2">
        {stepTitles.map((title, i) => (
          <div
            key={title}
            className={`flex-1 rounded border p-2 text-center text-xs ${
              stepIndex === i ? "border-blue-600 bg-blue-50 font-semibold" : "bg-white"
            }`}
          >
            {i + 1}. {title}
          </div>
        ))}
      </div>

      <div className="space-y-4 rounded border bg-white p-6">
        {current && (
          <>
            {current.description && (
              <p className="rounded bg-amber-50 px-3 py-2 text-sm text-amber-900">
                {current.description}
              </p>
            )}
            {current.fields.length === 0 && !current.description && (
              <p className="text-sm italic text-slate-500">Nothing to fill in here.</p>
            )}
            {current.fields.map((key) => (
              <FieldInput
                key={key}
                name={key}
                field={schema.properties[key]}
                value={values[key]}
                required={required.has(key)}
                onChange={(next) => setField(key, next)}
                piiPreview={piiPreview}
              />
            ))}
          </>
        )}

        {onReview && (
          <div className="space-y-3 text-sm">
            <h3 className="text-lg font-semibold">Review &amp; Submit</h3>
            <dl className="space-y-2">
              {Object.keys(schema.properties).map((key) => (
                <div key={key}>
                  <dt className="font-medium">{fieldTitle(key)}</dt>
                  <dd className="text-slate-700">
                    <ReviewValue value={values[key]} />
                  </dd>
                </div>
              ))}
            </dl>
            <button
              type="submit"
              disabled={submitting}
              className="mt-4 rounded bg-green-600 px-6 py-2 text-white hover:bg-green-700 disabled:opacity-50"
            >
              {submitting ? "Submitting…" : submitLabel}
            </button>
          </div>
        )}

        {errorBox}
      </div>

      <div className="mt-4 flex justify-between">
        <button
          type="button"
          onClick={back}
          disabled={stepIndex === 0}
          className="rounded border px-4 py-2 disabled:opacity-50"
        >
          Back
        </button>
        <button
          type="button"
          onClick={next}
          disabled={onReview}
          className="rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50"
        >
          Next
        </button>
      </div>
    </form>
  );
}
