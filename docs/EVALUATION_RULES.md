# KAVREN evaluation rules — `evaluator_version` 0.1.0

This is the normative definition of what the evaluator checks. The code in `kavren_core` implements it, and the fixtures in `fixtures/` are hand-checked against it. If the code and this document disagree, the code has a bug.

Status: **approved 3 Nov 2026.** Rejection truth (C2f) was approved by the owner on 3 Nov 2026.

## 1. Canonical JSON and hashes

- Canonical JSON: UTF-8, keys sorted, separators `,` and `:`, no whitespace, non-ASCII written as-is, **floats forbidden**.
- `spec_hash = sha256(canonical spec bytes)`.
- File hashes are `sha256` over the exact bytes of the file. No normalisation is applied before hashing.
- All hashes are lowercase hex.

## 2. Files

| File | Who makes it | Header |
|---|---|---|
| input file | buyer | template columns (any order, trimmed) |
| work file | protocol | `record_id`, then template columns in spec order |
| submission | provider | `record_id,_outcome,_merged_into,_reason`, then template columns in spec order |
| deliverable | protocol, after PASS | template columns only; kept non-canary rows only |

**Dialect, for every file:**
- UTF-8, no BOM
- comma delimiter, RFC 4180 quoting, `\n` line endings, header row required
- every row has exactly the header's number of fields
- size limit: 5 MB and 20,000 data rows

Values are always strings; there's no type inference. `record_id` is 12 lowercase hex characters, random and unique within a job.

## 3. Value rules

- **text** (`kavren-text/1`): the allowed transformations are trimming, collapsing internal whitespace runs to a single space, and changing letter case. Output must be already whitespace-normalised.
- **email** (`kavren-email/1`): the allowed transformations are trimming and lowercasing. Output must be lowercase and trimmed, and must match the documented pragmatic rule. See `formats.is_valid_email`: no leading, trailing or consecutive dots in the local part; at least two domain labels; an alphabetic TLD of 2–63 characters; ≤ 254 characters in total.
- **date:** the input is trimmed, then parsed with the spec's `input_formats`. A value counts as unparseable if no format matches, or if two formats give different dates. Output must be in `output_format` (`YYYY-MM-DD`).
- **Empty** means empty after trimming.

**Dedupe key:** the tuple of the key columns' normalised input values (email → trimmed and lowercased; text → whitespace-normalised and case-folded). A record with any empty key column has **no key**, and can never be merged or treated as a duplicate.

## 4. Checks

Each check runs independently and reports every violation it finds, as `(check, code, record_id)`. If C1 fails, C2–C8 and H1 are skipped (the file can't be interpreted). The verdict is PASS only if no check reports a violation.

| Check | Rule |
|---|---|
| **C1 Schema** | The file obeys the dialect; the header equals the submission header exactly; `_outcome` is one of `kept`, `merged`, `rejected` |
| **C2 Accounting** | (a) The set of submission `record_id`s equals the work file's set, each exactly once. (b) `kept` rows: `_merged_into` and `_reason` are empty. (c) `merged` rows: `_merged_into` names a `kept` record, `_reason` is empty, and data cells are empty. (d) `rejected` rows: `_reason` is one of the spec's reason codes, `_merged_into` is empty, and data cells are empty. (e) **Merge truth:** a merged record and its target have the same, non-empty dedupe key in the **work file**. (f) **Rejection truth:** the stated reason is true of the work-file record (see 4.1) |
| **C3 Duplicates** | No two kept rows share a dedupe key, computed on the **output** values. Each kept row whose key already appeared on an earlier kept row (in file order) is reported |
| **C4 Email** | Every non-empty email cell in a kept row is valid under the email rule **after normalisation**. Whether the cell was actually normalised is C6's job, so one problem isn't reported twice |
| **C5 Completeness** | Every required column is non-empty in every kept row |
| **C6 Format** | Kept rows: text cells are whitespace-normalised; email cells are lowercase and trimmed; non-empty date cells parse with `output_format` |
| **C7 Fidelity** | For every kept row and column, let G be the row plus every row merged into it. The output value must equal the allowed transformation of the **work-file** value of some member of G (text: equal after normalisation, ignoring case; email: equal after normalising both sides; date: the same calendar date). C7 checks *where a value came from*; C6 checks *its form*. The output may be empty only if every member of G is empty in that column. Date cells that already failed C6 are skipped here, so one problem isn't reported twice |
| **C8 Rejection cap** | `rejected × 100 ≤ max_reject_pct × total`, counting only the buyer's **real** records, never canaries, with integer arithmetic. Canaries are excluded because some of them *must* be rejected; counting them would use up the buyer's allowance on small files |
| **H1 Canaries** | Every canary expectation in the committed set holds (section 5). During a job, only a count of failures is reported |

### 4.1 Rejection truth (C2f)

| Reason | True when, for the work-file record |
|---|---|
| `MISSING_REQUIRED` | some required column is empty |
| `INVALID_EMAIL` | some email column is non-empty and its normalised value is invalid |
| `INVALID_DATE` | some date column is non-empty and can't be parsed |

**Why:** without this check, a provider could discard up to `max_reject_pct` of good records just by calling them invalid. **The evaluator never accepts the provider's stated reason as given.** It re-derives the reason from the work file's own values using the rules in section 3. Merges get the same treatment (C2e).

**Consequence (accepted by the owner):** for CSV, the public checks fully determine whether the cleaning is correct, so canaries (H1) are a second layer. Every canary failure in the fixtures is also caught by a public check.

## 5. Canaries

A canary is a synthetic record inserted into the work file. The committed set is:

```json
{"salt": "<64 hex>", "template_id": "...", "canaries": [{"record_id": "...", "row": {...}}],
 "expectations": [ ... ]}
```

`canary_commitment = sha256(canonical bytes of that object)`.

**Expectation types:**
- `{"type":"kept_with_values","record_id":R,"values":{col: exact output string}}`: R is kept, and each listed column equals the given value exactly.
- `{"type":"rejected_with_reason","record_id":R,"reason":CODE}`: R is rejected with exactly that code.
- `{"type":"duplicate_group","record_ids":[...]}`: exactly one member is kept, and every other member is merged into that one.

The deliverable for the buyer removes canary rows.

**Generation** (`kavren_core/canaries.py`): about 8 canaries per job, built from the input's own profile (email domains, date range):
- 2 messy-but-fixable records → `kept_with_values`
- 1 clean record → `kept_with_values`
- 1 duplicate pair → `duplicate_group`
- 1 each of an unfixable email, a missing required field and an impossible date → `rejected_with_reason`

Every expectation is self-checked against the same value rules the evaluator uses before the draft is issued. Canary keys never collide with the buyer's records. Record IDs, positions and the 256-bit salt come from a cryptographically secure random source.

**Winnable-job check:** a draft is refused if the buyer's file has more *unavoidable* rejections than the cap allows. A row is unavoidable when a rejection reason is true and it has no clean twin to merge into. Without this check, the protocol could create a job that no honest provider could ever pass.

## 6. Report

The report is canonical JSON:

```json
{"evaluator_version", "spec_hash", "work_hash", "output_hash", "canary_commitment",
 "verdict": "PASS|FAIL",
 "checks": [{"check": "C1".."H1", "status": "pass|fail|skipped", "violations": [{"code", "record_id"}]}],
 "canary_failures": int}
```

- **No cell values and no timestamps** appear in it.
- H1 `violations` is **always** empty; only `canary_failures` is given, so the report never identifies canaries, before or after the job ends. The detailed canary result is available to `kavren verify` after the reveal.
- Violations are sorted by code, then by work-file order.
- `report_hash = sha256(report bytes)`.

**Violation codes:**
- C1: `BAD_DIALECT`, `BAD_HEADER`, `BAD_OUTCOME`
- C2: `MISSING_RECORD`, `UNKNOWN_RECORD`, `DUPLICATE_RECORD`, `BAD_HELPER_FIELDS`, `BAD_MERGE_TARGET`, `MERGE_KEY_MISMATCH`, `REASON_NOT_TRUE`
- C3: `DUPLICATE_KEY`
- C4: `INVALID_EMAIL`
- C5: `MISSING_REQUIRED`
- C6: `NOT_NORMALISED`, `BAD_DATE_FORMAT`
- C7: `NOT_DERIVED_FROM_INPUT`
- C8: `REJECT_CAP_EXCEEDED`
- H1: `CANARY_FAILED`
