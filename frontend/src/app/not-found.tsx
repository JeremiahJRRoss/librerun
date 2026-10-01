import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center">
      <h1 className="text-4xl font-bold">404</h1>
      <p className="mt-2 text-slate-600">Page not found.</p>
      <Link href="/dashboard" className="mt-4 rounded bg-blue-600 px-4 py-2 text-white">
        Go home
      </Link>
    </div>
  );
}
