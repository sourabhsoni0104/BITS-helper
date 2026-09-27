# Engineering decisions

## 2026-09-24: zero-dependency vertical slice

The initial implementation uses Python's standard library so the academic engine,
tests, CLI, and dashboard run without a package download. This accelerates the
first end-to-end slice and keeps the deterministic boundary obvious.

The active data contract is a normalized JSON snapshot. The current ingestion
command validates and atomically publishes this format; PDF/table extraction is
still outstanding because no academic source documents were supplied.

The included demo snapshot is explicitly fictional and isolated under
`data/synthetic`. Real-data mode never falls back to it.

The natural-language parser is deliberately bounded and deterministic for the
source examples. An optional LLM adapter and broader typed parsing can be added
later without changing policy evaluation.

## 2026-09-24: profile persistence and extraction staging

Profiles use SQLite from the standard library. Updates increment a profile
version and reject stale writes, which keeps later cache invalidation and
multi-request behavior deterministic. Completed and in-progress attempts remain
distinct in storage and the dashboard.

Source ingestion is split into extraction and publication. Poppler extracts
page-aware PDF text; UTF-8 text fixtures can use form-feed page boundaries.
Extracted documents are always marked `review_needed`, and missing applicability
scope produces a review issue. They cannot become the active recommendation
snapshot until a separate normalized snapshot passes validation.

## 2026-09-27: verified claims require usable evidence

Snapshot publication now rejects verified courses, offerings, availability,
category memberships, handout facts, policy contexts, and requirements unless
they cite at least one verified evidence record. Evidence must also resolve to a
registered source document and contain a supporting excerpt. Requirement-rule
evidence is included in recommendation cards so the displayed contribution can
be traced to its source.

## 2026-09-27: authenticated handout index, public PDF archive

The course-handout listing requires an authenticated browser session, but its
540 PDF targets use direct BITS academic URLs. The application does not collect
or persist login credentials or browser cookies. Instead, the user saves the
authenticated index as HTML under ignored raw data, and a resumable downloader
parses the course metadata, allowlists the expected HTTPS host and path, validates
each PDF, and writes a hashed download manifest.

## 2026-09-27: runtime profiles and two document trust lanes

Student profile and academic-history data are runtime inputs, not a required
static corpus file. A marksheet parser may propose course-attempt fields, but its
output remains `review_needed`, preserves the source page and line, and does not
infer pass/fail semantics from a grade without an applicable grading rule.
Recognized runtime profile PDFs are deliberately excluded from the shared
document-search corpus to avoid exposing one student's records as general
reference material.

The repository's category-wise academic report is outside product scope. It is
excluded from inventory candidates and the search corpus, and the marksheet
importer rejects that format. Runtime profile import uses the performance sheet
and still requires review before saving parsed attempts.

Every reference PDF and DOCX under `data/` is also indexed in a deduplicated,
page- or logical-section-aware SQLite FTS
corpus for broader cited questions. Retrieved passages are labeled
`source_text_only`. General retrieval cannot directly create verified academic
rules or bypass the normalized snapshot used by the deterministic eligibility
engine.

## 2026-09-27: local accounts and explicit source publication

Real-data mode supports an ephemeral guest session by default and optional local
accounts. Guest profiles and pending marksheet reviews use in-memory storage only;
sign-out, expiry, or server restart makes them inaccessible and removes them.
Account passwords are salted and stretched, sessions and CSRF tokens are
server-validated, and each account is bound to one opaque persistent profile ID.
The first account is the local source-review administrator; guests and later
accounts cannot reach review or publication routes. This is a localhost deployment
model, not a claim of production internet hardening.

Extraction and trust remain separate. The generated review bundle contains
candidate records and evidence but makes no verified claims. An administrator can
compare each claim to the indexed page, mark evidence verified, edit structured
scope, and publish only the supported subset. Existing active data is archived
before atomic replacement.

## 2026-09-27: attempts are events, not course flags

Course history stores distinct attempt IDs, term IDs, and chronological ordering.
This preserves retakes and lets an evidence-backed repeat policy choose the latest
result. Runtime marksheet uploads live only in an expiring in-memory review until
the student confirms rows. Unknown course codes are not silently added to the
catalog, and letter grades do not infer academic status.

## 2026-09-27: conservative scheduling and document answers

Schedule checks report `unknown` when times, terms, or selected alternative
sections are unresolved. They do not merge all sections and manufacture a
conflict. Cited document answers are extractive complete passages with exact page
or DOCX-section links. They deliberately do not synthesize a new academic rule or
resolve source applicability.
