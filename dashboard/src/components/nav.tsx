"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/", label: "다이제스트" },
  { href: "/signals", label: "시그널" },
  { href: "/theses", label: "종목발굴" },
  { href: "/trends", label: "트렌드" },
  { href: "/analyses", label: "분석" },
  { href: "/events", label: "컨퍼런스" },
  { href: "/settings", label: "설정" },
];

export function Nav() {
  const pathname = usePathname();

  return (
    <header className="border-b border-light-gray bg-cream">
      <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
        <Link href="/" className="font-serif text-xl font-bold text-charcoal tracking-tight">
          시그널 캐처
        </Link>
        <nav className="flex gap-1">
          {links.map(({ href, label }) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={`px-3 py-1.5 rounded-full text-sm transition-colors ${
                  active
                    ? "bg-sage text-white"
                    : "text-warm-gray hover:bg-cream-dark"
                }`}
              >
                {label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
