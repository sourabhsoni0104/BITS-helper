# Codebase review — 2026-09-27

The project is now a working local-first academic workbench, not just a synthetic
recommendation demo. It has use-and-forget guest sessions, optional authentication,
private profiles, reviewed marksheet
import, full-library cited search, a source-review gate, and a deterministic
academic engine. A real recommendation dataset is deliberately **not active yet**:
`data/processed/review-bundle.json` is staged for human verification, while
`data/processed/active.json` remains absent.

The category-wise report is outside product scope and is excluded by content and
filename from inventory, document search, and runtime profile import.

## Current coverage

- 423 unique reference documents from 564 source paths: 402 handouts, 18 other
  references, one bulletin, one regulations document, and one timetable.
- 2,879 PDF pages or DOCX logical sections; zero empty indexed units after OCR.
- Incremental, content-hash-based indexing with duplicate-source preservation.
- Extractive answers with complete source passages, numbered citations, filters,
  and exact page/section links. Retrieval never promotes text into policy.
- A staged review bundle containing 513 course candidates, 540 offering
  candidates, and 1,568 source-linked evidence records.
- The supplied performance sheet parses into 24 reviewable attempt candidates.
  Unknown course mappings remain unselected and grades never imply pass/fail.

## Corrections implemented

| Area | Correction |
| --- | --- |
| Policy accounting | Programme and minor requirements use one allocation, aliases credit once, repeated courses follow an explicit latest-attempt policy, and shared credit requires every requirement to opt in. |
| Complexity | Requirement allocation uses bounded memoized state search rather than raw Cartesian enumeration. |
| Eligibility | Current/completed courses are excluded, prerequisite equivalences require verified evidence, and missing or conflicting facts stay unknown. |
| Scheduling | Meeting conflicts are detected only for the applicable term; absent schedules and ambiguous alternative sections return unknown. |
| Profiles | SQLite writes are concurrency-safe. Grades, units, terms, attempt IDs, and chronological order survive dashboard edits. |
| Sessions/accounts | Guest profiles and pending imports remain in memory and are deleted on session end. Optional account passwords use salted PBKDF2 hashes, sessions are stored by token hash, profile ownership is server-bound, CSRF protects mutations, and only the first local account is an administrator. |
| Student UI | Raw programme/minor IDs and CSV attempt editing were replaced with labelled selectors and structured add/remove course-history rows. Internal identifiers stay behind the form boundary. |
| Transcript import | Uploads are bounded and ephemeral. A latest BITS performance sheet pre-fills recognized student metadata and the complete extracted past/current course history. Student IDs decode admission year, supplied branch codes (including four-character dual degrees), and campus. |
| Documents | PDF and DOCX extraction, OCR fallback, incremental rebuilds, exact source navigation, and deduplicated extractive answers are implemented. |
| Review/publication | Extracted claims start as `needs_review`. Administrators verify evidence page by page; publication includes only evidence-supported records and archives the prior active snapshot. |
| Handout archive | The application never stores BITS credentials. It consumes a browser-saved authenticated index, allowlists download targets, validates PDFs, hashes results, and resumes safely. |
