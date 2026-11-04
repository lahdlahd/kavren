import type { Seal as SealT } from "@/lib/timeline.mjs";

export function Seal({ seal, stamp = false }: { seal: SealT; stamp?: boolean }) {
  return (
    <div className={`seal${stamp ? " stamp" : ""}`} data-tone={seal.tone} role="img" aria-label={`${seal.word}: ${seal.line}`}>
      <div>
        <strong>{seal.word}</strong>
        <span>{seal.line}</span>
      </div>
    </div>
  );
}
