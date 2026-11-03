# KAVREN evaluation rules — `evaluator_version` 0.1.0

This is the normative definition of what the evaluator checks. The code in `kavren_core` implements it, and the fixtures in `fixtures/` are hand-checked against it. If the code and this document disagree, the code has a bug.

Status: **draft for owner approval.** Items marked **[PROPOSED]** are refinements found while writing the answer key. They aren't approved yet.

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
| **C2 Accounting** | (a) The set of submission `record_id`s equals the work file's set, each exactly once. (b) `kept` rows: `_merged_into` and `_reason` are empty. (c) `merged` rows: `_merged_into` names a `kept` record, `_reason` is empty, and data cells are empty. (d) `rejected` rows: `_reason` is one of the spec's reason codes, `_merged_into` is empty, and data cells are empty. (e) **Merge truth:** a merged record and its target have the same, non-empty dedupe key in the **work file**. (f) **[PROPOSED] Rejection truth:** the stated reason is true of the work-file record (see 4.1) |
| **C3 Duplicates** | No two kept rows share a dedupe key, computed on the **output** values. Each kept row whose key already appeared on an earlier kept row (in file order) is reported |
| **C4 Email** | Every non-empty email cell in a kept row is valid under the email rule **after normalisation**. Whether the cell was actually normalised is C6's job, so one problem isn't reported twice |
| **C5 Completeness** | Every required column is non-empty in every kept row |
| **C6 Format** | Kept rows: text cells are whitespace-normalised; email cells are lowercase and trimmed; non-empty date cells parse with `output_format` |
| **C7 Fidelity** | For every kept row and column, let G be the row plus every row merged into it. The output value must equal the allowed transformation of the **work-file** value of some member of G (text: equal after normalisation, ignoring case; email: equal after normalising both sides; date: the same calendar date). C7 checks *where a value came from*; C6 checks *its form*. The output may be empty only if every member of G is empty in that column. Date cells that already failed C6 are skipped here, so one problem isn't reported twice |
| **C8 Rejection cap** | `rejected rows / all rows × 100 ≤ max_reject_pct`, computed over all rows including canaries, with integer arithmetic: `rejected × 100 ≤ max_reject_pct × total` |
| **H1 Canaries** | Every canary expectation in the committed set holds (section 5). During a job, only a count of failures is reported |

### 4.1 [PROPOSED] Rejection truth (C2f)

| Reason | True when, for the work-file record |
|---|---|
| `MISSING_REQUIRED` | some required column is empty |
| `INVALID_EMAIL` | some email column is non-empty and its normalised value is invalid |
| `INVALID_DATE` | some date column is non-empty and can't be parsed |

**Why this was added:** without it, a provider can reject up to `max_reject_pct` of good records by falsely claiming they're invalid. Canaries would catch that only if a canary happened to be among those records. **The consequence:** for this CSV category, the public checks then fully determine whether the cleaning was correct, so canaries (H1) become a second layer that is redundant with them. That's fine (see the Phase 0 report), but it should be a deliberate choice.

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

## 6. Report

The report is canonical JSON: `{evaluator_version, spec_hash, work_hash, output_hash, verdict, checks:[{check, passed, violations:[{code, record_id}]}], canary_failures: int}`.

- **No cell values and no timestamps** appear in it.
- In the report for an unfinished job, H1 `violations` is always empty and only `canary_failures` is given.
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
