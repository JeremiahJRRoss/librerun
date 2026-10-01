import Link from "next/link";

export default function Breadcrumb({ items }: { items: { href?: string; label: string }[] }) {
  return (
    <nav className="px-6 py-2 text-xs text-slate-600">
      {items.map((it, i) => (
        <span key={i}>
          {it.href ? (
            <Link href={it.href} className="hover:underline">
              {it.label}
            </Link>
          ) : (
            <span className="text-slate-900">{it.label}</span>
          )}
          {i < items.length - 1 && <span className="mx-2">›</span>}
        </span>
      ))}
    </nav>
  );
}
