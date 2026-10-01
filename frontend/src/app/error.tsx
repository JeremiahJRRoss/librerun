"use client";

import Link from "next/link";

export default function GlobalError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center">
      <h1 className="text-4xl font-bold">Something went wrong</h1>
      <p className="mt-2 text-sm text-slate-600">{error.message}</p>
      <div className="mt-4 flex gap-2">
        <button onClick={reset} className="rounded border px-4 py-2">Retry</button>
        <Link href="/dashboard" className="rounded bg-blue-600 px-4 py-2 text-white">Go home</Link>
      </div>
    </div>
  );
}
