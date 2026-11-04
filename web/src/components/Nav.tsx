"use client";
import { usePathname } from "next/navigation";

const LINKS = [
  ["/jobs/", "Jobs"],
  ["/attacks/", "Attack gallery"],
  ["/owner/", "Owner console"],
] as const;

export function Nav() {
  const path = usePathname() || "/";
  return (
    <nav className="site-nav" aria-label="Main">
      {LINKS.map(([href, label]) => (
        <a key={href} href={href} aria-current={path.startsWith(href.slice(0, -1)) ? "page" : undefined}>{label}</a>
      ))}
    </nav>
  );
}
