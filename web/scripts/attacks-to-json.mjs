// Build step: turn ../docs/ATTACKS.md (from the live attack gallery run) into JSON for the page.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseAttacks } from "../src/lib/attacks.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, "..", "..", "docs", "ATTACKS.md");
const out = join(here, "..", "src", "generated", "attacks.json");
mkdirSync(dirname(out), { recursive: true });
const data = existsSync(src) ? parseAttacks(readFileSync(src, "utf8")) : { missing: true, rows: [], money: {} };
writeFileSync(out, JSON.stringify(data, null, 2) + "\n");
console.log(existsSync(src) ? `attacks: ${data.rows.length} rows from docs/ATTACKS.md` : "attacks: docs/ATTACKS.md not found");
