# BITS Academic Course Recommender

> Detailed implementation brief and coding-model context for **Postman Round 2**.
> Derived from the supplied four-page `postman_25_r2.pdf`.
> The repository now contains a tested local-first workbench; the original detailed specification remains below.

## Current implementation and quick start

The zero-dependency Python application now includes:

- A validated, versioned normalized-JSON snapshot contract with atomic publication.
- Typed profiles, attempts, courses, offerings, evidence, requirements, and handout facts.
- Scoped policy resolution, requirement accounting, and pass/fail/unknown prerequisite evaluation.
- A bounded deterministic parser for the source brief's example requests.
- Eligibility-first hard/soft preference matching with explicit unknown handling.
- A server-rendered local workbench with ephemeral guest mode, optional accounts,
  and an isolated synthetic demo.
- Human-readable campus, degree, minor, and academic-term selectors plus a normal
  add/remove course-history table; internal policy IDs remain hidden.
- Per-account SQLite profiles, session authentication, CSRF protection, ownership
  enforcement, and version-conflict protection.
- Transcript-first setup: the latest BITS performance sheet fills student metadata,
  all recognized past/current courses, grades, units, and terms into the profile;
  the original PDF is discarded after parsing.
- Student IDs decode admission year, the supplied two-character branch codes,
  integrated dual-degree pairs such as `B3A7`, and the final campus code.
- Incremental PDF and DOCX ingestion, OCR fallback, duplicate tracking, and a
  SQLite full-text corpus with exact source navigation.
- Extractive document answers with complete passages, numbered citations, and
  document-type/semester filters.
- A source-review dashboard and atomic reviewed-subset publication with history.
- Minor composition, verified equivalences, explicit credit sharing, repeat
  policy, bounded allocation, and conservative schedule-conflict checks.
- 113 unit and live HTTP integration tests.

See [the codebase review](docs/codebase-review.md) for corrected bugs, remaining
limitations, and the prioritized feature roadmap.

The data folder contains BITS regulations, a bulletin, the current timetable,
prerequisite/reference documents, DOCX files, and the archived course handouts.
The corpus currently contains 423 unique documents and 2,879 pages or logical
sections, with no empty indexed units after OCR. A 513-course/540-offering source
review draft is staged at `data/processed/review-bundle.json`; it remains
unpublished until a person verifies applicability and supporting claims. Real-data
mode never substitutes the synthetic fixture. Student history is supplied at
runtime and is excluded from the shared reference corpus.

Run from the repository root with Python 3.11 or newer:

```bash
# Tests and snapshot validation (no dependency installation required)
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m recommender.cli validate \
  --data data/synthetic/demo_snapshot.json
PYTHONPATH=src python3 -m recommender.cli inventory \
  --input data --report data/reports/source-inventory.json

# Incrementally index every reference PDF/DOCX and ask cited questions
PYTHONPATH=src python3 -m recommender.cli build-corpus \
  --input data --output data/processed/document-corpus.sqlite
PYTHONPATH=src python3 -m recommender.cli answer-docs \
  --query 'What is the make-up application process?'

# Rebuild the candidate review draft (never auto-publishes)
PYTHONPATH=src python3 -m recommender.cli prepare-review

# Clearly labeled fictional demo
PYTHONPATH=src python3 app/dashboard.py --demo

# Authenticated real-data workbench
PYTHONPATH=src python3 app/dashboard.py
```

Then open `http://127.0.0.1:8501`. Choose **Continue as guest** for use-and-forget
mode: the profile and uploaded marksheet review stay only in server memory and are
deleted on “Forget this session”, expiry, or server restart. Choose sign-in only
when you want a profile saved locally for later visits. The first locally
registered account becomes the source-review administrator. It can inspect
citations, mark evidence verified, edit the structured snapshot, and publish the
reviewed subset. Until that review is complete, profiles, marksheet import, and
document answers work while academic recommendations correctly remain unavailable.

For a separately prepared normalized snapshot, publication still validates and
atomically replaces the active file:

```bash
PYTHONPATH=src python3 -m recommender.cli ingest \
  --input /path/to/candidate.json \
  --output data/processed/active.json
```

PDF extraction uses `pdfinfo` and `pdftotext` from Poppler; optional Tesseract OCR
recovers image-only pages. DOCX files are represented by logical sections rather
than invented physical page numbers. Extract one confirmed source with explicit
applicability scope:

```bash
PYTHONPATH=src python3 -m recommender.cli extract \
  --input data/raw/example-handout.pdf \
  --type handout \
  --campus '<campus-from-source>' \
  --semester '<term-from-source>' \
  --output data/extracted/example-handout.json
```

Extraction does not make academic claims automatically: outputs remain
`review_needed`. The complete reference corpus may answer broader questions
through cited retrieval,
but source text does not become an eligibility rule until it is reviewed and
published through the normalized snapshot contract.

A performance marksheet can propose runtime profile attempts without being
treated as a trusted policy source. The repository's category-wise report is
outside product scope and is excluded from ingestion and search:

```bash
PYTHONPATH=src python3 -m recommender.cli parse-marksheet \
  --input /path/to/marksheet.pdf \
  --output data/extracted/marksheet-review.json
```

The output always requires review. Extracted grades remain unresolved until the
applicable grading policy defines their completion semantics.

The authenticated Pilani handout index can be saved from the browser as an HTML
page, then archived without storing login credentials in the application:

```bash
PYTHONPATH=src python3 -m recommender.cli download-handouts \
  --index 'data/raw/All Courses Handouts.html' \
  --output data/raw/handouts \
  --manifest data/reports/handout-downloads.json
```

The downloader accepts only HTTPS PDF URLs under the expected BITS handout path,
runs sequentially with a delay, validates PDF signatures, records SHA-256 hashes,
and skips valid files on a rerun. The browser-saved HTML can contain account
metadata, so keep it under the already ignored `data/raw/` directory and never
commit it.

## 0. Start here: instructions for the model building this project

Build a working, data-driven dashboard that helps a BITS student select academically valid courses for a semester. First determine the student's applicable requirements and eligibility. Only then match eligible courses to the student's natural-language preferences. Every academic claim must be supported by the supplied academic data.

Read this document as three distinct layers:

| Label | Meaning | How to use it |
| --- | --- | --- |
| **SOURCE REQUIREMENT** | Explicitly stated in the supplied project brief | Required project behavior; use the source map below to verify it |
| **IMPLEMENTATION RECOMMENDATION** | Engineering design proposed in this README | Sensible default; may be replaced if the required behavior is preserved |
| **ILLUSTRATIVE ONLY** | Example schema, fixture, pseudocode, command, or response | A design aid, never evidence of a real BITS rule, course, or existing implementation |

Unless explicitly identified as a source requirement, the detailed architecture, schemas, endpoints, algorithms, tests, and build sequence below are implementation recommendations. They elaborate the brief without creating new institutional policy or claiming extra organizer requirements.

### 0.1 Non-negotiable implementation invariants

1. **Academic validity comes before preference matching.** A semantic match cannot override a failed prerequisite, restriction, or applicable programme rule.
2. **The supplied BITS documents are the academic source of truth.** General model knowledge is not a replacement for missing documents.
3. **Pre-process first.** Runtime recommendations must use validated structured records, not repeated whole-PDF prompts.
4. **Missing evidence remains unknown.** A missing attendance paragraph does not establish that attendance is optional. A missing exam row does not establish that no midsem exists.
5. **Rules are contextual.** Campus, batch, programme, dual-degree combination, semester, and academic history can change applicability.
6. **Separate facts from interpretations.** A documented makeup procedure is a fact; calling it "lenient" is an interpretation that needs an explicit criterion.
7. **Keep provenance.** Academic facts and derived decisions must be traceable to source document locations.
8. **Calculate live results.** Do not hardcode course lists, remaining requirements, eligibility outcomes, or generated answers.
9. **Make uncertainty visible.** Do not present an unverified course as a verified recommendation.
10. **Timetable intelligence is optional.** Complete the core recommender before spending effort on schedule optimization.

### 0.2 What is and is not available

The attachment supplied for this README is the project brief only. It describes five input classes that the project will be supplied with:

- BITS Academic Regulations.
- BITS Bulletin.
- Current-semester timetable.
- Course handouts for available courses.
- Student profile / academic history.

Their actual contents are not included in the brief. Consequently, this README does **not** establish real requirement totals, allowed course categories, prerequisite rules, grade thresholds, unit limits, course offerings, faculty policies, or semester schedules.

When the academic files are missing, implement the ingestion interfaces, validation, rule engine, dashboard, and clearly labeled synthetic tests. Show a missing-data state in real-data mode. Do not fill production tables with plausible-looking BITS facts.

### 0.3 Small-context model usage

If the whole README fits in context, supply it together with the actual academic files and repository state. If it does not, always supply Sections 0-3 plus the relevant task sections:

| Work item | Additional sections |
| --- | --- |
| Data model and ingestion | 5-8 |
| Requirement accounting and eligibility | 6, 8-10 |
| Query parsing and ranking | 9-13 |
| Dashboard and API | 14-15 |
| Timetable bonus | 16 |
| Repository setup and implementation | 4, 17-19 |
| Validation and final review | 20-23 |

Ask the model to implement one vertical slice at a time. Require it to report changed files, actual checks run, remaining gaps, and unsupported assumptions. Do not accept a polished UI as evidence that the underlying academic logic works.

## 1. Source requirements and traceability

All references below use the PDF's printed page numbers, which also correspond to its four PDF pages. The source filename is `postman_25_r2.pdf`; the title is **BITS Academic Course Recommender**, subtitled **Postman Round 2**.

| ID | Source requirement or guidance | Source location |
| --- | --- | --- |
| SRC-01 | Build an agentic AI-based semester course recommender for BITS students | Section 1, p. 1 |
| SRC-02 | Apply the student's programme, batch, and academic-history requirements before interest-based recommendations | Sections 1 and 5, pp. 1-2 |
| SRC-03 | Use the supplied regulations, bulletin, timetable, handouts, and student history; do not invent unsupported facts | Section 2, p. 1 |
| SRC-04 | Pre-process relevant information into clean structured data; retrieve from that representation | Section 3, pp. 1-2 |
| SRC-05 | Normalize course codes and categories; mark unreliable extractions for verification | Section 3, p. 2 |
| SRC-06 | Support creating and updating campus, admission year, degree/dual degree, current semester, completed/current courses, minor, and interests | Section 4, p. 2 |
| SRC-07 | Calculate remaining CDC, DEL, HUEL, and OPEL requirements and apply prerequisites, restrictions, and programme rules | Section 5, p. 2 |
| SRC-08 | Follow requirement analysis, remaining requirements, eligibility, preferences, policy validation, and final recommendations in that order | Section 5, p. 2 |
| SRC-09 | Accept natural-language requests through the dashboard and explain why recommendations satisfy them | Section 6, p. 3 |
| SRC-10 | Use handout properties such as attendance, evaluations, projects, quizzes, makeup policy, instructor, and topics | Section 7, p. 3 |
| SRC-11 | State when a requested property cannot be verified from supplied data | Section 7, p. 3 |
| SRC-12 | Optional bonus: consider class/tutorial/lab and midsem/compre conflicts, time preferences, and alternative sections | Section 8, p. 3 |
| SRC-13 | Keep academic checks deterministic wherever possible; use the LLM mainly for intent, semantic matching, and explanation | Section 9, p. 3 |
| SRC-14 | Preserve source references and validate extracted codes, prerequisites, categories, and requirements | Section 9, p. 3 |
| SRC-15 | Accept new semester timetables and handouts without changing recommendation logic | Section 9, p. 3 |
| SRC-16 | Keep final recommendations concise: requirement satisfied, eligibility, relevant properties, and match rationale | Section 9, p. 3 |
| SRC-17 | Deliver a working dashboard with live computation from processed data; do not hardcode results | Section 10, p. 4 |
| SRC-18 | Deliver a clean Git repository with application, preprocessing, retrieval, and clear README setup/run instructions | Section 10, p. 4 |

The entity table in the PDF is explicitly a **possible structure**. Its concepts matter; its exact storage format is not mandated. The PDF does not prescribe a programming language, cloud provider, framework, database, vector database, model vendor, or multi-agent framework.

## 2. Product goal and scope

### 2.1 Primary user journey

1. A student creates or updates their academic profile.
2. The application resolves the applicable academic rules.
3. It calculates completed and remaining requirements from the student's record.
4. The student enters a course-selection query.
5. The application determines which currently available courses are academically eligible.
6. It applies the query's requested category and course-property preferences.
7. It validates the proposed result against applicable policies.
8. It returns concise recommendations with reasons and source references.
9. If information is missing, conflicting, or insufficient, it explains the specific limitation instead of guessing.

### 2.2 Required core versus optional extensions

| Core project | Optional or later enhancement |
| --- | --- |
| Profile creation and editing | Institutional login and transcript import |
| Structured document ingestion | Administrative extraction-review UI |
| Applicable academic-rule resolution | Sophisticated policy-authoring interface |
| Remaining-requirement calculation | Multi-semester graduation planning |
| Prerequisite and restriction checks | Full semester schedule optimization |
| Natural-language preferences | Voice input or conversational memory |
| Handout-grounded matching | Vector search when simpler retrieval is insufficient |
| Source-backed explanations | Deployment automation and monitoring dashboards |
| Live dashboard and reproducible repository | Registration-system integration |

The recommender advises on selection. Actual enrollment, seat availability, waitlists, or registration transactions are outside the supplied brief unless separately requested and supported by additional data.

### 2.3 Terminology

Use the category identifiers `CDC`, `DEL`, `HUEL`, and `OPEL` exactly and normalize source aliases carefully. The brief does not define their expansions or precise counting rules; obtain these from the actual regulations and bulletin rather than model memory.

Other terms used here:

- **Programme context:** the student's applicable programme or combination of programmes, campus, batch, and relevant academic period.
- **Course:** a catalog-level academic entity.
- **Offering:** a course made available in a particular semester and campus, potentially with multiple sections.
- **Eligibility:** whether the student meets the documented conditions for a particular offering or course under the applicable rules.
- **Requirement satisfaction:** how a completed or proposed course contributes to a particular curriculum obligation.
- **Preference match:** how well an academically eligible course meets the student's requested properties.
- **Evidence:** a source-backed fact with document and location references.
- **Verification issue:** a missing, ambiguous, conflicting, or unreliable fact that affects a result.

Eligibility, requirement satisfaction, current availability, and preference matching are separate decisions. Do not collapse them into one boolean or one similarity score.

## 3. Authority, unknowns, and decision semantics

### 3.1 Authority rules

The project brief defines product behavior. Actual supplied academic documents define institutional facts. This README proposes implementation mechanics. A model's general knowledge has no authority to invent BITS policy.

When academic sources disagree:

1. Check whether they apply to different campuses, batches, programmes, semesters, instructors, or sections.
2. Check whether a supplied source explicitly supersedes another source.
3. Apply a documented precedence rule only when there is evidence for it.
4. If the conflict remains, preserve both references and flag verification.

Do not assume the newest filename wins, that regulations always resolve every course-specific issue, or that one campus's bulletin applies to another. These may be reasonable hypotheses to investigate, but they are not established rules in the attachment.

### 3.2 Distinguish value from evidence quality

Store a field's value separately from its verification status. For example:

```json
{
  "field": "midsem_present",
  "value": null,
  "verification_status": "not_found",
  "evidence_ids": [],
  "reason": "No explicit midsem statement was extracted from the supplied handout."
}
```

Suggested verification statuses:

- `verified`: validated source evidence supports the value.
- `needs_review`: evidence was extracted but is uncertain or incomplete.
- `not_found`: the supplied material did not establish this field.
- `conflicting`: applicable evidence contains unresolved disagreement.

Suggested decision statuses:

- `pass`: available evidence supports satisfaction of the check.
- `fail`: available evidence establishes that the check is not satisfied.
- `unknown`: the check cannot be resolved from available evidence.

Avoid using `false`, `0`, an empty list, or an empty string as a generic replacement for unknown. An explicit empty prerequisite list means something different from an unparsed prerequisite field.

### 3.3 Unknown-handling policy

| Situation | Recommended behavior |
| --- | --- |
| A prerequisite is explicitly satisfied | Eligibility check passes |
| A prerequisite is explicitly unsatisfied | Eligibility check fails |
| A prerequisite or relevant completion record is unresolved | Eligibility is unknown; do not call it verified eligible |
| A strict "no midsem" request has explicit evidence of no midsem | Preference constraint passes |
| A strict "no midsem" request has explicit evidence of a midsem | Preference constraint fails |
| A strict "no midsem" request has missing exam evidence | Exclude from verified matches; optionally show under unverified alternatives |
| A soft preference has unknown evidence | Keep eligibility separate; give no verified match credit for that preference |
| Programme requirements cannot be resolved | Do not claim the remaining requirement totals are correct |
| A known hard disqualifier and an unrelated unknown both exist | Overall candidate fails; preserve both check details |

Never silently relax a hard request. If no exact matches remain, explain which constraint eliminated candidates and invite an explicit relaxation. Academically ineligible candidates must never become recommendations through preference relaxation.

## 4. Reference architecture

**IMPLEMENTATION RECOMMENDATION:** Use a small modular application with a deterministic academic engine and a bounded LLM-assisted orchestration layer.

| Component | Responsibility | Must not do |
| --- | --- | --- |
| Document ingestion | Extract useful facts and preserve locations | Invent facts to complete records |
| Structured data store | Hold validated records, scope, evidence, and versions | Mix synthetic and real data invisibly |
| Profile service | Validate and persist student academic state | Treat an in-progress course as completed |
| Policy resolver | Select rules applicable to the profile and period | Apply every campus/batch rule globally |
| Requirement engine | Calculate completed, allocated, and remaining obligations | Count a course twice without policy support |
| Eligibility engine | Evaluate prerequisites, restrictions, and availability | Let an LLM override failed checks |
| Intent parser | Convert user language into a typed query | Generate arbitrary SQL or academic rules |
| Retrieval/ranking | Retrieve eligible records and score supported matches | Retrieve only semantically similar courses before eligibility |
| Final validator | Recheck the proposed course or set | Trust a generated explanation as proof |
| Explanation renderer | Present verified facts, reasons, and citations | Add facts absent from the result object |
| Dashboard | Collect inputs and display computed results | Contain a separate hardcoded academic engine |

### 4.1 A pragmatic implementation option

For a new repository with no existing stack, one workable option is:

- Python for parsing, typed models, and deterministic services.
- SQLite for an initial normalized data store, with explicit migrations.
- Pydantic or an equivalent schema validator for typed boundaries.
- Streamlit for a fast dashboard implementation.
- An LLM provider behind a small adapter for structured intent and optional explanation.
- Pytest or an equivalent runner for rules, accounting, and integration tests.
- PDF text/table extraction tooling with OCR only where needed.

This stack is a recommendation, not a source requirement. A React/FastAPI implementation or an existing project stack is also acceptable. Preserve existing project conventions when appropriate. A separate HTTP API, vector database, graph database, or agent framework is not necessary merely to satisfy the word "agentic."

Pin and document the actual dependency versions chosen during implementation. Do not copy a speculative "latest" version into the repository without checking compatibility.

### 4.2 Practical meaning of agentic

Implement a controller that can interpret a query, call structured-data and academic-analysis functions, respond to missing information, and produce a validated result. The controller should expose a useful trace of actions and check results, not private model reasoning.

Suggested tool/service contracts:

```text
load_profile(profile_id) -> StudentProfile
resolve_policy_context(profile, semester_id) -> PolicyContext
analyze_requirements(profile, policy_context) -> RequirementAnalysis
parse_query(query, known_categories, allowed_fields) -> QueryIntent
retrieve_offerings(campus, semester_id) -> Offering[]
evaluate_eligibility(profile, offering, policy_context) -> EligibilityResult
match_preferences(eligible_offerings, intent) -> MatchResult[]
validate_selection(profile, proposed_selection, policy_context) -> ValidationResult
render_recommendations(validated_result) -> UserFacingAnswer
```

A deterministic controller may own ordering while the LLM handles intent and semantic judgments. The model must not be able to bypass validation by choosing a different tool order.

## 5. Data layout and ingestion workflow

### 5.1 Preserve raw input and publish a validated snapshot

Use separate logical layers:

1. **Raw inputs:** original source files and their checksums.
2. **Extracted content:** page-aware text, tables, and candidate facts.
3. **Normalized records:** typed entities with canonical codes and scope.
4. **Validation reports:** malformed fields, unresolved references, conflicts, and review items.
5. **Published snapshot:** the validated dataset used by the live recommender.

Do not partially overwrite the live dataset during ingestion. Build a candidate snapshot, validate it, and activate it as one consistent version. If ingestion fails, preserve the previously active valid snapshot and report the failure.

### 5.2 Document registry

Register each source with:

| Field | Purpose |
| --- | --- |
| `document_id` | Stable internal identifier |
| `document_type` | Regulations, bulletin, timetable, handout, or profile/history |
| `file_name` and `content_hash` | Original identity and reproducibility |
| `campus_scope` | Campus applicability, if established |
| `programme_scope` | Programme applicability, if established |
| `batch_scope` | Admission-year/batch applicability |
| `semester_scope` | Relevant academic term |
| `publication_date` / `effective_period` | Only when supported by the source |
| `parser_version` | Extraction reproducibility |
| `ingested_at` | Operational timestamp |
| `status` | Extracted, review needed, validated, or superseded |

Unknown scope must be represented explicitly. Do not treat a missing campus field as "all campuses."

### 5.3 Extraction sequence

1. Detect whether pages have usable text or require OCR.
2. Extract page-level text and tables while retaining location information.
3. Identify relevant academic sections and tables.
4. Extract candidate facts with their original text and references.
5. Normalize course codes, category names, dates, times, units, and identifiers.
6. Parse prerequisite expressions without losing logical operators or exceptions.
7. Attach contextual scope and provenance to each fact.
8. Validate schema, foreign keys, semantic consistency, and evidence coverage.
9. Write unresolved issues to a review report.
10. Publish the validated snapshot and retrieval indexes.

Retain the original source even when irrelevant material is removed from retrieval. Footnotes and exceptions may contain governing policy; do not discard them just because they are short or separated from a table.

### 5.4 Normalization rules

- Keep both original and canonical course codes.
- Normalize harmless spacing/case differences with a documented function.
- Preserve meaningful suffixes, campus distinctions, and equivalent-course relationships.
- Do not use fuzzy string similarity alone to merge two course identities.
- Map category aliases to canonical identifiers only when justified.
- Store units as numbers without assuming they are always integers.
- Store course-count requirements separately from unit requirements.
- Preserve raw timetable slot labels until a supplied legend resolves them.
- Normalize times to an explicit representation with campus-local semantics.
- Preserve AND, OR, nested groups, minimum grades, and permission conditions in prerequisite text.
- Distinguish absent sections from sections that explicitly state "none."

### 5.5 Ingestion quality checks

Produce an ingestion report showing record counts and actionable issues, including:

- Course references that do not resolve to a known canonical identity.
- Duplicate identities with conflicting properties.
- Programme rules with missing scope or missing thresholds.
- Handouts without a resolvable course/offering association.
- Timetable rows with invalid or unresolved times.
- Prerequisites that could not be converted into a supported expression.
- Source-backed academic facts without page/section references.
- Evaluation weights that appear inconsistent when a complete weighted breakdown is explicitly expected.
- Contradictory exam-presence or attendance statements.
- Records excluded from the active dataset and why.

Not every unusual value is an error. For example, do not force evaluation weights to sum to 100 if the handout uses points, alternatives, bonus components, or an incomplete breakdown.

### 5.6 Semester updates

Adding a new timetable or handout set should require ingestion/configuration changes, not rewriting recommendation logic. Version offerings and handout facts by term. A course's old evaluation pattern must not silently become its current pattern.

Keep a dataset version in every recommendation result. Cache keys should include the active dataset version and relevant profile/intent state so stale recommendations are invalidated after updates.

## 6. Suggested structured data model

These are conceptual entities; related tables may be combined for a small implementation as long as the distinctions survive.

### 6.1 StudentProfile and CourseAttempt

| Entity | Suggested fields |
| --- | --- |
| `StudentProfile` | `profile_id`, `campus`, `admission_year`, `programme_ids`, `current_semester`, `target_semester_id`, `minor_id`, `interests`, `profile_version` |
| `CourseAttempt` | `attempt_id`, `profile_id`, `course_id`, `term_id`, `status`, `grade`, `units_awarded`, `evidence_ids` |

`programme_ids` should support more than one degree without flattening the combination into a guessed single programme. `minor_id` may be absent when not applicable. Store grade or credit detail when supplied and required by an applicable rule; do not require irrelevant personal information.

Distinguish statuses such as `completed`, `in_progress`, `failed`, and `withdrawn` if those states exist in the input. Interpret successful completion using supplied academic rules. A course appearing in a transcript is not automatically a passed course.

Keep confirmed completed credits separate from projected completion of current courses. Do not assume an in-progress prerequisite will be satisfied before registration unless the policy supports that treatment.

### 6.2 Course, Offering, and Section

| Entity | Suggested fields |
| --- | --- |
| `Course` | `course_id`, `canonical_code`, `source_codes`, `title`, `department`, `units`, `topics`, `evidence_ids` |
| `Offering` | `offering_id`, `course_id`, `campus`, `semester_id`, `availability_status`, `handout_id`, `evidence_ids` |
| `Section` | `section_id`, `offering_id`, `section_label`, `component_type`, `instructor`, `meetings`, `linked_section_constraints`, `evidence_ids` |

Course identity and offering identity should be separate. Course titles, instructors, evaluation components, and class times can vary by offering. Preserve section-specific handout differences when supplied.

Do not infer current availability merely because a course exists in a historic bulletin. Use the current-semester supplied material and flag uncertainty if that material does not establish availability.

### 6.3 ProgrammeRule and Requirement

| Entity | Suggested fields |
| --- | --- |
| `ProgrammeRule` | `rule_id`, `rule_type`, `applicability`, `expression`, `effective_period`, `verification_status`, `evidence_ids` |
| `Requirement` | `requirement_id`, `programme_context_id`, `category`, `metric`, `required_value`, `course_pool`, `mandatory_course_ids`, `allocation_constraints`, `evidence_ids` |
| `CourseCategoryMembership` | `course_id`, `category`, `programme_scope`, `batch_scope`, `campus_scope`, `effective_period`, `evidence_ids` |
| `CreditAllocation` | `attempt_id`, `requirement_id`, `credited_amount`, `allocation_rule_id`, `evidence_ids` |

Use a contextual membership relationship instead of assuming every course has one universal category. A course's usefulness as a DEL, HUEL, or OPEL must follow the supplied programme rules.

Represent requirement metrics explicitly: course count, units, mandatory-course completion, or a supported combination. If multiple metrics constrain one category, evaluate all applicable constraints.

Do not invent a default total when `required_value` is unknown. Unknown requirement totals should remain unknown in the dashboard and result object.

### 6.4 Prerequisite and restriction expressions

Store a typed expression tree. Example **syntax only**, using synthetic identifiers:

```json
{
  "op": "all_of",
  "conditions": [
    {"op": "completed", "course_id": "SYNTHETIC-FOUNDATION-A"},
    {
      "op": "any_of",
      "conditions": [
        {"op": "completed", "course_id": "SYNTHETIC-FOUNDATION-B"},
        {"op": "completed", "course_id": "SYNTHETIC-FOUNDATION-C"}
      ]
    }
  ]
}
```

Support only operators whose semantics are implemented and tested. Possible operators include `completed`, `minimum_grade`, `all_of`, `any_of`, `programme_in`, `minimum_semester`, and `permission_required`. Add operators only when the supplied data requires them.

Keep prerequisites, corequisites, exclusions, and permission conditions distinct. An unsupported condition should produce `unknown` / review needed, not an unconditional pass.

Three-valued combination rules:

- `all_of`: fail if any child fails; pass if every child passes; otherwise unknown.
- `any_of`: pass if any child passes; fail if every child fails; otherwise unknown.
- If negation is supported: swap pass/fail and preserve unknown.

Define empty-list semantics explicitly. Never represent "prerequisites not extracted" as an empty `all_of` that evaluates to true.

### 6.5 HandoutFacts

| Field | Representation guidance |
| --- | --- |
| Attendance policy | Raw text plus structured conditions; allow conditional or unknown |
| Midsem presence | Explicit true/false/unknown with evidence |
| Compre presence | Explicit true/false/unknown with evidence |
| Evaluation components | Type, weight/points, mandatory status, timing, and evidence |
| Project-based evaluation | Presence and documented weight; avoid unsupported importance claims |
| Quizzes | Documented presence, count/frequency when supplied |
| Makeup policy | Conditions, approvals, documentation, deadlines, and exclusions |
| Instructor | Offering/section-specific when needed |
| Syllabus/topics | Source-derived topics; semantic tags labeled as derived |

Attendance rules can distinguish mandatory attendance, attendance marks, minimum percentages, participation requirements, or lab-specific conditions. Do not flatten them prematurely into a single `has_attendance` boolean.

### 6.6 Evidence and review items

Each meaningful academic fact should reference evidence with:

```text
evidence_id
document_id
pdf_page_number
printed_page_label (if different)
section_heading
table_or_row_locator (when applicable)
supporting_excerpt
extraction_method
verification_status
extraction_confidence (optional and method-specific)
```

Confidence is not proof. A model-generated confidence score does not resolve missing evidence or source conflicts. Keep extraction reliability, semantic relevance, and academic validity as separate concepts.

Review items should identify the affected entity/field, issue type, source references, and downstream impact. For example: "Unresolved prerequisite for this offering; eligibility cannot be verified."

## 7. Student profile behavior

**SOURCE REQUIREMENT:** The dashboard must allow profile creation and updates with at least campus, admission year, degree/dual degree, current semester, completed/current courses, minor if applicable, and academic interests.

Recommended validations:

- Require enough context to select applicable programme and batch rules.
- Validate course identifiers against the structured catalog or explicitly mark unmatched entries.
- Detect duplicated attempts or contradictory current/completed status.
- Preserve multiple attempts rather than blindly deduplicating all repeats.
- Allow an empty completed-course history for a new student.
- Distinguish current academic semester number from the target term identifier if the data requires both.
- Mark unverified profile imports instead of assuming OCR was perfect.

When a profile changes, invalidate previous requirement analysis and recommendations. Show which saved profile version produced a result.

Ask only for missing information that changes the decision. For example, missing batch can block rule resolution; missing interests need not block a query that already specifies the desired topic.

## 8. Remaining academic requirement analysis

This step precedes recommendation ranking.

### 8.1 Required computation sequence

1. Resolve the student's applicable campus/programme/batch policy context.
2. Load all relevant requirements and source-backed category memberships.
3. Identify which academic attempts qualify as completed under those rules.
4. Resolve equivalences, substitutions, repeats, and exclusions only where supplied policy supports them.
5. Allocate recognized completions to requirements subject to documented allocation rules.
6. Calculate each requirement's total, credited progress, and remaining amount.
7. Preserve outstanding mandatory courses separately from aggregate totals.
8. Report unknowns and unsupported cases that prevent a verified calculation.

### 8.2 Accounting rules

For a simple additive requirement with a known total and valid allocation:

```text
remaining = max(required_amount - credited_amount, 0)
```

This formula is not a substitute for allocation logic. Before subtracting, determine what counts, in which category, in which metric, and whether overlapping credit is allowed.

Do not:

- Subtract a number of courses from a unit total.
- Treat every completed course as fulfilling an outstanding requirement.
- Count an in-progress course as confirmed earned credit.
- Count a repeated or equivalent course twice without support.
- Automatically count one course toward multiple degree/minor categories.
- Infer that aggregate units eliminate a specific mandatory-course obligation.
- Assume a category is unrestricted just because its name suggests flexibility.

If source rules permit multiple allocations, find an allocation that respects all constraints and document the tie-breaking objective. A greedy allocation can falsely leave requirements unsatisfied. For a small dataset, deterministic enumeration or constrained matching may be sufficient; an optimizer is optional.

### 8.3 Requirement result contract

Each requirement result should include:

```text
requirement_id
category
metric
required_amount
credited_amount
remaining_amount
outstanding_mandatory_courses
credited_attempt_ids
allocation_rule_ids
status: verified | incomplete | conflicting
evidence_ids
warnings
```

If the applicable total is unknown, `remaining_amount` must not display as zero. An unknown requirement and a completed requirement are different states.

### 8.4 Current versus projected progress

A useful dashboard may show both:

- **Confirmed progress:** based on recognized completed work.
- **Projected progress:** conditional on successful completion of specified current or proposed courses.

Label projections clearly and cite any rules needed to count them. The brief does not require projected-progress functionality, so it should not delay a correct confirmed-progress calculation.

## 9. Eligibility engine

### 9.1 Candidate evaluation

Start from the relevant campus and target-semester offerings. For each candidate, evaluate all applicable checks, such as:

1. Offering availability.
2. Programme and batch applicability.
3. Prerequisites.
4. Corequisites, if documented.
5. Registration restrictions or exclusions.
6. Completed/current/repeat-course treatment under supplied rules.
7. Category eligibility for the requested requirement.
8. Any additional documented academic constraint relevant to selection.

This list describes potential rule families, not a statement that every BITS programme has each rule. Only activate policy conditions established in the supplied material.

### 9.2 Explainable output

Return structured checks rather than one opaque boolean:

```text
EligibilityResult:
  course_id
  offering_id
  overall_status: pass | fail | unknown
  checks[]:
    check_id
    rule_id
    status
    reason_code
    short_explanation
    evidence_ids
    relevant_profile_fields
  applicable_requirement_ids[]
  unresolved_issues[]
```

Useful internal reason codes include `PREREQUISITE_UNMET`, `RULE_SCOPE_UNRESOLVED`, `CATEGORY_NOT_APPLICABLE`, `OFFERING_UNVERIFIED`, and `PERMISSION_UNVERIFIED`. These are application identifiers, not official BITS terminology.

### 9.3 Final validation

Run policy validation again on the proposed recommendation payload before displaying it. This catches stale data, mismatched offering IDs, invalid category claims, and set-level constraints.

A list of individually eligible alternatives is different from a proposed semester bundle. If the application says "take these together," validate relevant combined-unit, co-requisite, duplication, and other supplied set-level constraints. If it only presents alternatives, label them as alternatives and do not imply joint feasibility.

## 10. Natural-language query parsing

### 10.1 Source examples to support

The brief includes these requests:

- "Suggest DELs related to AI."
- "I want an OPEL with no attendance requirement."
- "Suggest courses with no midsem and a lenient makeup policy."
- "I need a HUEL and prefer project-based evaluation."
- "Suggest an AI-related DEL with no midsem."

The model should convert each into a structured intent, then retrieve and evaluate records. It should not answer from memory.

### 10.2 Suggested query schema

Example **ILLUSTRATIVE ONLY**, representing the user's request rather than asserting any course satisfies it:

```json
{
  "requested_category": "DEL",
  "requirement_id": null,
  "topic_preferences": ["AI"],
  "hard_constraints": [
    {"field": "midsem_present", "operator": "eq", "value": false}
  ],
  "soft_preferences": [],
  "schedule_preferences": [],
  "requested_count": null,
  "ambiguities": [],
  "original_query": "Suggest an AI-related DEL with no midsem."
}
```

Allowlist fields and operators. Validate types and limits. A parsed query is untrusted input until validated. Do not execute model-written SQL, Python, shell commands, or rule expressions.

### 10.3 Hard constraints versus soft preferences

Recommended interpretation defaults:

| Language | Suggested interpretation |
| --- | --- |
| "must", "only", "no midsem", "without attendance requirement" | Hard preference constraint, unless context clearly indicates otherwise |
| "prefer", "ideally", "would like" | Soft preference |
| "AI-related" | Semantic topic preference; category/eligibility still checked separately |
| "easy", "lenient", "low workload" | Ambiguous subjective request; clarify or use an explicitly disclosed, evidence-based proxy |

Show a compact interpretation summary so the student can correct it. Never reinterpret "no attendance requirement" as merely "attendance is not graded" without explaining the difference.

### 10.4 Ambiguity handling

For "lenient makeup policy," identify what the handout actually states: documentation, approval, deadline, allowed reasons, or automatic eligibility. If the student's intended meaning affects filtering, ask a focused clarification.

If no objective criterion can be established, return policy facts without asserting leniency. Similarly, do not equate project-based assessment with an easy course or fewer workload hours.

If an LLM is unavailable or returns invalid output, preserve deterministic profile and requirement functionality. Show a clear parser-unavailable state or a documented limited fallback; do not fabricate a successful natural-language interpretation.

## 11. Retrieval and preference matching

### 11.1 Retrieval order

1. Resolve student context and requirements.
2. Retrieve offerings relevant to campus and semester.
3. Evaluate eligibility and contextual requirement/category membership.
4. Build the verified eligible set.
5. Apply hard user constraints using verified field evidence.
6. Rank the remaining candidates by supported soft preferences and relevance.
7. Validate the result.
8. Render concise explanations.

For efficiency, safe deterministic prefilters may be pushed into database queries. Their result must be equivalent to the rule engine's semantics. Do not take a global semantic top-k first and assume it contains all academically relevant candidates.

### 11.2 Structured retrieval versus semantic retrieval

Use exact structured queries for course identifiers, offerings, categories, units, known exam presence, attendance conditions, and eligibility checks.

Use semantic matching for topics and interests where wording differs. For example, a source-derived topic can be semantically related to the student's query even when it does not use the exact phrase. Label that relationship as a semantic match and preserve the underlying source topics.

Embeddings are optional. If used, index concise course/topic representations with metadata filters and evidence IDs. A vector score establishes textual similarity, not prerequisite satisfaction or academic approval.

### 11.3 Ranking design

Recommended ordering:

1. Academic checks must pass.
2. Explicit hard preferences must pass.
3. Prefer candidates serving the student's requested or outstanding requirement.
4. Rank by supported topic and soft-property matches.
5. Use a stable tie-breaker such as canonical course code.

Weights are application design choices, not academic rules. Make any weights configurable and test that changing them cannot admit a failed or unknown academic candidate.

Do not award a match for a field that is unknown. Avoid deceptive score normalization that makes candidates with mostly missing data look perfect. Include evidence coverage or a match breakdown alongside the score if scores are shown.

### 11.4 No-result behavior

Distinguish these outcomes:

- No applicable rules could be resolved.
- No eligible offerings are available in the supplied data.
- Eligible offerings exist, but none satisfy a hard preference.
- Possible matches exist, but the requested property cannot be verified.
- A schedule-feasible combination could not be found, if scheduling is implemented.

Report the actual cause. Offer a specific next step, such as supplying a handout, correcting a profile field, or relaxing an explicitly identified preference. Do not quietly replace a requested DEL with an unrelated category.

## 12. Recommendation response contract

Recommended top-level fields:

```text
request_id
profile_id
profile_version
dataset_version
target_semester_id
interpreted_query
requirement_summary
recommendations[]
unverified_alternatives[]
no_result_reason
clarification_questions[]
warnings[]
```

Each verified recommendation should include:

```text
course_id / course_code / title
offering_id
requirement_satisfied
category_for_this_student
eligibility_status
eligibility_reasons
matched_preferences
unmatched_soft_preferences
relevant_handout_properties
section_choice (optional)
schedule_validation_status
evidence_references
```

Use precise status values for schedule validation, such as `not_implemented`, `not_checked`, `verified_feasible`, `infeasible`, or `unknown`. If schedule intelligence is absent, do not call results clash-free.

### 12.1 Recommended card format

The following is a template, not a factual course recommendation:

```text
<Course code> - <Course title>
Requirement: <the applicable requirement this course can satisfy>
Eligibility: <verified prerequisite/restriction result>
Matches: <specific supported topic or evaluation/attendance properties>
Unverified: <requested facts that remain unknown, if any>
Schedule: <checked result or explicitly not checked>
Sources: <document, page/section, and supporting fact>
```

Keep the default card concise, with expandable supporting checks. The brief calls for concise final recommendations even though this implementation document is detailed.

### 12.2 Explanation safeguards

- Generate explanations only from the validated result object and retrieved evidence.
- Attach evidence to individual claims or tightly grouped claims.
- Do not cite a document merely because it is generally related to the course.
- Distinguish "eligible for this student" from "counts toward this requirement."
- Do not describe unspecified course properties as favorable.
- Do not let generated text change the selected course IDs, category allocation, or eligibility status.
- Prefer deterministic text templates for factual claims; use the LLM for concise phrasing only when useful.
- If generated text fails a factual consistency check, fall back to the deterministic renderer.

## 13. Bounded LLM orchestration and cost control

### 13.1 Appropriate LLM tasks

- Converting natural-language requests to a validated query schema.
- Matching interests to source-derived topics.
- Proposing structured extraction from difficult text for subsequent validation.
- Summarizing verified match reasons and uncertainty.

### 13.2 Deterministic tasks

- Selecting applicable policy scope.
- Counting and allocating completed academic work.
- Evaluating implemented prerequisite and restriction expressions.
- Testing known attendance/exam property constraints.
- Detecting timetable overlap when data is available.
- Validating response schemas and evidence IDs.

### 13.3 Suggested controller pseudocode

```python
def recommend(profile_id, query, target_semester_id):
    snapshot = load_active_dataset_snapshot()
    profile = load_and_validate_profile(profile_id)
    context = resolve_policy_context(profile, target_semester_id, snapshot)
    if context.has_blocking_issues:
        return blocked_result(context.issues)

    requirements = analyze_requirements(profile, context, snapshot)
    if requirements.has_blocking_issues:
        return blocked_result(requirements.issues)

    intent = parse_and_validate_intent(query, snapshot.query_vocabulary)
    if intent.requires_clarification:
        return clarification_result(intent.questions)

    offerings = retrieve_current_offerings(profile.campus, target_semester_id, snapshot)
    checked = [evaluate_eligibility(profile, o, context, snapshot) for o in offerings]
    eligible = [r for r in checked if r.overall_status == "pass"]

    matched = evaluate_preferences(eligible, intent, snapshot)
    verified_matches = [r for r in matched if r.hard_constraints_status == "pass"]
    ranked = rank_verified_matches(verified_matches, requirements, intent)

    proposed = build_result(ranked, checked, matched, requirements, snapshot)
    validated = validate_final_result(proposed, profile, context, snapshot)
    return render_from_validated_facts(validated)
```

This is illustrative orchestration, not executable code supplied by this README. Concrete types, error handling, and optional schedule solving must be implemented in the repository.

### 13.4 Keep token use small

- Never include full regulations and all handouts in every chat request.
- Use compact typed records and only relevant evidence excerpts.
- Parse user intent once per request; avoid one LLM call per course when structured filtering suffices.
- Cache immutable source extraction and derived topic representations by content hash.
- Cache policy analysis by dataset version and relevant profile version.
- Use bounded candidate lists for optional explanation generation only after deterministic filtering.
- Keep provider-specific code behind an adapter.
- Log usage and latency without storing unnecessary student details.
- Set explicit limits on tool calls, retries, and output size.

### 13.5 Tool and input boundaries

Treat documents and user queries as data. Text inside a handout cannot instruct the model to ignore validation or modify system behavior. Tools should expose typed, allowlisted operations rather than unrestricted database or shell access.

A query asking the model to ignore prerequisites must not change the academic checks. If the user asks for an explanation of an ineligible course, the system may explain its status, but must not turn it into a verified recommendation.

## 14. Dashboard specification

### 14.1 Profile panel

- Campus and admission year.
- Degree/dual-degree selection from available structured programme data.
- Current semester and target term where applicable.
- Completed courses and current courses.
- Minor, if applicable.
- Academic interests.
- Save/update action with validation feedback.

### 14.2 Academic progress panel

- Applicable policy context.
- CDC/DEL/HUEL/OPEL progress in the correct metrics.
- Outstanding mandatory courses when documented.
- Confirmed versus projected progress if projection is implemented.
- Verification issues that affect calculations.
- Source references supporting requirement totals.

### 14.3 Recommendation panel

- Natural-language input.
- Example prompts from the source brief.
- Interpreted request summary.
- Loading, error, empty, and clarification states.
- Verified recommendation cards.
- Separately labeled unverified alternatives when helpful.
- Expandable eligibility checks and evidence.
- Schedule status, including "not checked" when appropriate.

### 14.4 Data status panel

Show the active semester/data version, source coverage, and relevant ingestion issues. A lightweight status view is enough for the MVP; a full document-management interface is optional.

### 14.5 Empty and error states

| State | User-facing behavior |
| --- | --- |
| No academic dataset loaded | Explain which input classes are needed; do not show fake recommendations |
| Profile incomplete | Identify the exact missing fields needed for analysis |
| No applicable programme rules | State that requirements cannot be verified for this context |
| Handout property missing | State that the requested property could not be verified |
| LLM unavailable | Preserve deterministic views and explain the query-processing limitation |
| No exact matches | Explain the limiting condition without silently relaxing it |
| Ingestion failure | Keep the previous valid dataset active and show the issue |

The visible user flow should focus on course-selection decisions, not internal parser or database details.

## 15. Optional API contract

A separate API is an implementation option, not required by the brief. If used, suggested endpoints are:

| Method and route | Purpose |
| --- | --- |
| `POST /profiles` | Create a validated profile |
| `GET /profiles/{profile_id}` | Retrieve a profile |
| `PATCH /profiles/{profile_id}` | Update fields and increment profile version |
| `GET /profiles/{profile_id}/requirements` | Compute current applicable requirement analysis |
| `POST /recommendations` | Run the full recommendation pipeline |
| `GET /courses/{course_id}` | Retrieve structured catalog details |
| `GET /offerings/{offering_id}` | Retrieve offering-specific details |
| `GET /evidence/{evidence_id}` | Retrieve authorized source metadata/excerpt |
| `GET /data/status` | Report dataset version and validation coverage |

Example request **shape only**:

```json
{
  "profile_id": "PROFILE-ID-FROM-DATABASE",
  "target_semester_id": "TERM-ID-FROM-DATASET",
  "query": "I need a HUEL and prefer project-based evaluation."
}
```

Use typed errors for invalid profiles, missing datasets, unresolved policies, unavailable LLM services, and internal failures. A valid request with zero matches is normally a valid result with an explanation, not an internal server error.

If the app serves multiple users, enforce profile ownership and source access. Do not expose arbitrary server file paths through evidence endpoints.

## 16. Optional bonus: timetable intelligence

**SOURCE REQUIREMENT CLASSIFICATION:** This is explicitly an optional advanced feature / brownie point in the brief.

### 16.1 Scope of checks

- Lecture/class overlaps.
- Tutorial overlaps.
- Lab overlaps.
- Midsem clashes.
- Comprehensive-examination clashes.
- Alternative section selection.
- No 8 AM classes.
- A selected weekday kept free.
- Avoiding long gaps.
- Compact schedules.

### 16.2 Meeting representation

For each section component, store day/date or recurrence, start time, end time, room when supplied, and the relevant term/week pattern. Store exam events separately with actual date/slot semantics established by the timetable legend.

An unresolved slot code is not a known free period. Missing exam data should produce an unknown exam-clash status, not a guaranteed clash-free claim.

For comparable intervals on an overlapping day/date pattern:

```text
overlap = (start_a < end_b) and (start_b < end_a)
```

This is an interval model, not a BITS-specific policy. It treats touching endpoints as non-overlapping. Apply any documented transition-time requirements separately; do not invent travel buffers as academic rules.

### 16.3 Section-aware solving

Evaluate valid bundles of lecture/tutorial/lab sections. Respect documented linked-section restrictions; do not assume every tutorial can be paired with every lecture.

If section A conflicts with a required CDC but section B is feasible, retain the course using section B. Do not reject the course after checking only the first section.

For several proposed courses, solve the combination jointly. Pairwise feasibility against the existing timetable does not prove that the recommended courses are mutually compatible.

### 16.4 Hard constraints and optimization

Enforce documented feasibility conditions first. Then optimize soft preferences such as fewer early classes, fewer long gaps, or more compact days. A student can make a preference hard, but that cannot override academic constraints.

For small inputs, backtracking with early pruning can be sufficient. A constraint solver is optional. Return the chosen sections and clear infeasibility reasons; do not expose an unexplained numerical schedule score as the only result.

## 17. Suggested repository organization

The following paths are proposed modules to implement, not files already supplied:

| Path | Responsibility |
| --- | --- |
| `README.md` | Verified setup, run instructions, architecture, limitations |
| `.env.example` | Non-secret configuration template |
| `pyproject.toml` | Python project/dependency configuration if using Python |
| `app/dashboard.py` | Dashboard entry point |
| `src/recommender/config.py` | Settings and feature flags |
| `src/recommender/models/` | Typed domain and request/response models |
| `src/recommender/ingestion/` | Document-specific parsers, normalization, validation |
| `src/recommender/storage/` | Database schema, migrations, repositories |
| `src/recommender/policies/` | Scope resolution, rule evaluation, requirement allocation |
| `src/recommender/retrieval/` | Structured queries and optional semantic matching |
| `src/recommender/llm/` | Provider adapter, intent parser, bounded explanation |
| `src/recommender/services/` | Recommendation orchestration |
| `src/recommender/scheduling/` | Optional section/schedule logic |
| `src/recommender/cli.py` | Ingestion, validation, and data-status commands |
| `data/raw/` | Supplied source inputs, kept out of public commits when appropriate |
| `data/processed/` | Versioned generated records or database artifacts |
| `data/reports/` | Validation and extraction review reports |
| `tests/fixtures/synthetic/` | Clearly labeled, isolated test data |
| `tests/unit/` | Policy, accounting, and constraint tests |
| `tests/integration/` | End-to-end data-to-recommendation tests |
| `docs/decisions.md` | Engineering decisions and unresolved questions |

Keep academic logic in services rather than UI callbacks. Avoid implementing different rule behavior in the dashboard, API, and tests.

## 18. Setup and run contract

The commands in "Current implementation and quick start" are implemented and
verified. The subsections below retain the longer-term target where it differs
from the current zero-dependency slice.

### 18.1 Current quick start

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m recommender.cli validate --data data/synthetic/demo_snapshot.json
PYTHONPATH=src python3 app/dashboard.py --demo
```

An editable install is also supported by `pyproject.toml`, but is not required.
The current dashboard uses the Python standard library rather than Streamlit.

### 18.2 Configuration

```dotenv
APP_DATA_PATH=data/processed/active.json
APP_PROFILE_DB=data/processed/profiles.sqlite
APP_CORPUS_DB=data/processed/document-corpus.sqlite
DEMO_MODE=false
APP_HOST=127.0.0.1
APP_PORT=8501
```

These variables are implemented by the dashboard. Never commit student academic
records. Demonstration mode uses an in-memory profile database and a conspicuous
label; real-data mode never falls back to demo data when ingestion fails.

### 18.3 Final README requirements

Before delivering the implemented repository, replace speculative setup instructions with verified commands and include:

- Supported runtime version and pinned dependency installation.
- Required input files and how to arrange them.
- Ingestion and validation commands.
- How to inspect and resolve extraction issues.
- Dashboard startup and configured access URL/port.
- LLM configuration and behavior when the service is unavailable.
- Test command and test scope.
- How to ingest a new semester without changing business logic.
- Implemented optional features and explicit limitations.
- Data-handling instructions for private student records.

Do not state "all tests pass" unless the tests were actually run. Do not claim production readiness merely because a local synthetic demo works.

## 19. Implementation plan with completion gates

### Phase 1: Inventory and contracts

Inspect the repository and supplied inputs. Record which of the five academic input classes are present. Define typed entities, evidence semantics, policy scope, and the real-data versus synthetic-data boundary.

**Gate:** The implementation can identify missing input classes and has no fabricated institutional defaults.

### Phase 2: Structured ingestion

Implement document registration, page-aware extraction, normalization, validation, and dataset publication. Start with a representative subset, but inventory and handle all relevant supplied material before calling ingestion complete.

**Gate:** A source-backed fact can be traced from input page to stored record and back. Unreliable facts enter a review path.

### Phase 3: Profiles and academic engine

Implement profile creation/update, applicable-rule resolution, requirement accounting, prerequisite logic, and contextual category eligibility.

**Gate:** Deterministic tests distinguish completed/current courses, AND/OR prerequisites, programme/batch differences, and unsupported rules. Unknowns do not become passes.

### Phase 4: Natural-language retrieval and ranking

Implement typed intent parsing, hard/soft preference handling, eligible-set retrieval, supported semantic matching, and no-result diagnostics.

**Gate:** The source example queries produce computed, evidence-grounded behavior on a controlled dataset. No-match and missing-handout cases are handled correctly.

### Phase 5: Live dashboard

Connect the UI to the real services. Add profile editing, progress display, recommendations, evidence, and useful error states.

**Gate:** Changing a profile or active dataset changes the result through the pipeline, without editing UI literals.

### Phase 6: Final validation and documentation

Verify final output claims, complete the source-requirement checklist, run the documented setup path, and write truthful setup/run instructions.

**Gate:** Another developer can reproduce ingestion and a live recommendation from the documented inputs. Remaining limitations are explicit.

### Phase 7: Optional timetable bonus

Add interval normalization, section bundles, conflict checks, and schedule preferences only after the core gates pass.

**Gate:** An alternate feasible section is selected instead of incorrectly rejecting the course, and missing timetable information remains unknown.

## 20. Test plan and acceptance criteria

Use synthetic fixtures for isolated behavior tests, clearly marked as fictional. Use actual supplied files for representative extraction/integration validation when available. Fixture outcomes prove implementation behavior, not real BITS academic policy.

### 20.1 Academic engine tests

| Test | Expected behavior |
| --- | --- |
| Same query, different programme | Applicable categories/rules are recomputed |
| Same programme, different batch | Correct batch-specific rules are selected |
| Missing relevant batch rule | Unknown/blocking state; no guessed total |
| Empty completed history | Correct source-backed full remaining obligations |
| Current prerequisite course | Not treated as completed without a supporting rule |
| Failed attempt | Not counted as passed merely because it appears in history |
| AND prerequisite with one failed child | Overall prerequisite fails |
| OR prerequisite with one passed child | Overall prerequisite passes |
| OR prerequisite with no pass and one unknown | Overall prerequisite remains unknown |
| Unknown prerequisite extraction | Candidate is not verified eligible |
| Dual-degree overlap | Allocation follows supplied overlap rules |
| Repeat/equivalent courses | No unauthorized double credit |
| Course-count versus unit requirement | Metrics remain separate |
| Mandatory course outstanding despite sufficient aggregate units | Mandatory obligation remains visible |
| Multiple valid category allocations | Selected allocation respects all constraints |

### 20.2 Handout and preference tests

| Test | Expected behavior |
| --- | --- |
| Explicit no-midsem evidence | Can satisfy a strict no-midsem constraint |
| Missing midsem evidence | Does not satisfy that constraint as verified |
| Midsem exists | Excluded from strict no-midsem matches |
| Attendance ungraded but compulsory | Does not satisfy "no attendance requirement" |
| Attendance policy differs by lab/lecture | Preserve the conditional policy |
| Project component exists | Report documented presence/weight; do not invent workload |
| Makeup text exists but no leniency criterion | Report facts or clarify; do not assert leniency |
| Unsupported "easy course" request | No invented difficulty rating |
| Strong AI relevance but failed prerequisite | Never recommended as academically valid |
| No exact matches | Explain cause; no silent constraint relaxation |

### 20.3 Ingestion and runtime tests

| Test | Expected behavior |
| --- | --- |
| Course-code spacing variation | Canonicalizes while preserving original code |
| Similar but distinct course codes | Not merged by fuzzy similarity alone |
| Contradictory applicable sources | Conflict preserved and flagged |
| New-semester handout | Current offering uses current validated facts |
| Failed ingestion | Previous valid snapshot remains active |
| Profile update | Cached academic analysis is invalidated |
| Dataset update | Cached recommendations are invalidated |
| Invalid LLM JSON or unknown operator | Rejected/handled without bypassing checks |
| Prompt injection inside source text | Treated as document content, not instructions |
| LLM service failure | Clear limited functionality; no fabricated response |
| Evidence reference in final answer | Resolves and supports its associated claim |

### 20.4 Optional scheduling tests

| Test | Expected behavior |
| --- | --- |
| First section conflicts, second is feasible | Choose the feasible section |
| Lecture fits but required lab conflicts | Section bundle is infeasible |
| Classes fit but exams clash | Do not label schedule fully feasible |
| Exam slot unresolved | Exam feasibility remains unknown |
| Two recommendations conflict with one another | Do not present them as a jointly feasible bundle |
| Week patterns do not overlap | Avoid a false conflict |
| Touching intervals | Apply documented interval/transition semantics |
| Free-day preference cannot be met | Explain failure or soft-preference tradeoff explicitly |

### 20.5 End-to-end evidence of live behavior

A strong integration demonstration should:

1. Ingest a small, controlled source set.
2. Create a student profile.
3. Compute remaining requirements.
4. Ask one of the brief's example queries.
5. Return source-backed eligible matches or a justified no-match result.
6. Modify a relevant completed-course record.
7. Show that eligibility changes through recomputation.
8. Modify or replace an applicable handout in a new dataset snapshot.
9. Show that matching changes through re-ingestion.
10. Confirm the result identifies the correct profile and dataset versions.

Tests should target meaningful failure modes. A test that simply repeats an implementation constant does not prove academic correctness.

## 21. Common failure modes to prevent

| Failure | Why it violates the goal | Better approach |
| --- | --- | --- |
| Prompting a model with all PDFs for every query | Expensive, inconsistent, weakly structured retrieval | Extract once and query validated records |
| Ranking globally by similarity before checking eligibility | Can omit valid candidates and surface invalid ones | Build the eligible set first |
| Storing one global category per course | Can ignore student-specific applicability | Contextual category membership |
| Assuming missing attendance text means no attendance | Converts absence of evidence into a claim | Explicit unknown status |
| Treating all transcript rows as completion | Can count failed/current/repeated attempts incorrectly | Attempt-aware, policy-aware accounting |
| Double-counting dual-degree or minor credit | Can understate remaining obligations | Source-backed allocation constraints |
| Hardcoding the four example query answers | Fails live-data requirement | Parse queries and compute results |
| Using the previous semester's handout silently | Can recommend on stale evaluation rules | Version facts by offering/term |
| Labeling policy facts as "easy" or "lenient" | Adds unsupported judgments | State facts or define a disclosed criterion |
| Rejecting a course after its first section clashes | Misses alternate feasible sections | Check valid section bundles |
| Calling independent alternatives a feasible semester plan | Ignores combined constraints | Validate a proposed set jointly |
| Hiding missing data behind a polished answer | Misleads the student | Explain exact verification gaps |
| Claiming setup commands work before implementing them | Misrepresents project readiness | Run and document actual commands |

## 22. Copy-ready prompts for cheaper coding models

These prompts are engineering instructions authored for this README. Supply the referenced sections and repository/data context with them.

### 22.1 Master implementation prompt

```text
Implement the BITS Academic Course Recommender described in this README.

Treat the attached project brief as the product specification and the supplied
academic files as the only authority for BITS-specific rules and course facts.
The README's architecture is a suggested design, not extra institutional policy.

First inspect the existing repository and inventory actual input files. Preserve
useful existing code. If the academic files are absent, implement interfaces and
isolated synthetic fixtures, and show an honest missing-data state in real mode.
Do not invent requirement totals, prerequisites, offerings, or handout properties.

Build in this order: typed models and provenance; ingestion and validation;
profiles; policy resolution and requirement accounting; eligibility; typed query
parsing; preference matching; final validation; dashboard; verified documentation.
Timetable intelligence is optional and comes after the core works.

Academic rule evaluation must be deterministic wherever possible. Unknown facts
remain unknown. All recommendations must come from processed data, and preference
scores must never override failed academic checks. Keep source references with
every academic claim. Do not hardcode course lists or query answers.

Work in small, testable increments. After each increment, report changed files,
checks actually run, remaining gaps, and decisions needing real academic evidence.
Do not claim functionality or passing tests that have not been verified.
```

### 22.2 Ingestion task prompt

```text
Implement the structured ingestion layer using README Sections 3 and 5-6.
Preserve raw inputs, source scope, page/section evidence, and original course codes.
Normalize into typed records; validate identities, prerequisites, categories,
requirements, offering associations, and handout properties. Keep extraction
uncertainty and source conflicts explicit. Publish only a validated snapshot.
Return an ingestion report and meaningful extraction tests. Do not guess facts
to make records complete, and do not modify recommendation policy to fit bad data.
```

### 22.3 Academic engine task prompt

```text
Implement policy resolution, requirement accounting, and eligibility using
README Sections 3 and 6-9. Separate completed from current attempts, units from
course counts, contextual categories from catalog identity, and eligibility from
requirement contribution. Use pass/fail/unknown checks with evidence and reason
codes. Preserve AND/OR prerequisites and unsupported-rule uncertainty. Prevent
unauthorized double counting. Test the identified edge cases with fictional,
explicitly labeled fixtures. Do not invent any BITS-specific totals or rules.
```

### 22.4 Query and recommendation task prompt

```text
Implement the query-to-recommendation pipeline using README Sections 10-13.
Parse user intent into a validated allowlisted schema. Determine academic
eligibility before matching preferences. Keep hard constraints separate from
soft preferences. Missing evidence must not satisfy a strict condition. Explain
no-match cases without silently relaxing the request. Generate explanations only
from validated result fields and evidence. A semantic score cannot override an
academic failure. Keep provider-specific LLM code behind an adapter.
```

### 22.5 Dashboard task prompt

```text
Implement the dashboard using README Section 14 and the existing service contracts.
Support profile creation/update, remaining requirements, natural-language queries,
verified recommendations, evidence details, and explicit uncertainty/error states.
All lists and outcomes must be computed from the active structured dataset.
Do not add mock answers to make the UI look finished. Label synthetic demo mode.
Invalidate results on relevant profile or dataset changes. Keep recommendation
cards concise and distinguish academic eligibility from schedule validation.
```

### 22.6 Final review prompt

```text
Review the implementation against every source requirement in README Section 1.
Inspect code and run relevant checks; do not rely only on screenshots or README
claims. Trace one recommendation from source evidence through structured records,
requirement analysis, eligibility, preference matching, and final validation.
Check unknown handling, batch/programme scope, double counting, hardcoded outputs,
stale data, invalid LLM output, and truthful setup instructions. Classify findings
as verified complete, incomplete, or unverifiable with available inputs. Report
concrete files and failures; do not invent missing evidence or claim tests ran.
```

## 23. Final completion checklist

### Required product behavior

- [ ] A student can create and update every profile field required by the brief.
- [ ] The application selects rules applicable to the student's context.
- [ ] Remaining CDC/DEL/HUEL/OPEL obligations are calculated from supplied data.
- [ ] Prerequisites and restrictions are checked before preference ranking.
- [ ] Source example queries are supported through live computation.
- [ ] Handout properties are used with correct evidence and uncertainty semantics.
- [ ] Requested properties that cannot be verified are identified explicitly.
- [ ] The final recommendation explains requirement contribution, eligibility, and match.
- [ ] Academic decisions and properties have usable source references.
- [ ] No academic facts, course lists, or answers are hardcoded as substitutes for data.

### Required technical deliverables

- [ ] A working dashboard is included.
- [ ] A clean Git repository includes application code.
- [ ] Preprocessing and normalization code is included.
- [ ] Runtime structured retrieval code is included.
- [ ] Extracted codes, prerequisites, categories, and requirements are validated.
- [ ] New semester timetables/handouts can be ingested without rewriting recommendation logic.
- [ ] Actual setup, ingestion, validation, and run commands are documented and checked.
- [ ] Missing inputs and unimplemented behavior are stated honestly.

### Optional timetable bonus

- [ ] Class/tutorial/lab and exam clashes are handled where data permits.
- [ ] Alternative sections are considered before rejecting a course.
- [ ] Time/day/compactness preferences are supported.
- [ ] A proposed combined schedule is validated jointly.
- [ ] Unknown or unchecked feasibility is not presented as clash-free.

## 24. Source coverage and document limitations

This README covers all ten sections of the supplied brief: objective, inputs,
preprocessing, profile, recommendation behavior, natural-language queries,
handout use, optional timetable intelligence, implementation guidance, and final
deliverables. Its additional design detail is intended to reduce ambiguity for
smaller coding models.

It is not an official BITS regulations document and does not replace the academic
sources the finished application needs. The unresolved work is to ingest those
actual sources, establish their applicability, implement the repository, and
verify the resulting live behavior. The central requirement remains: determine
what the student is required and eligible to take before recommending what best
matches their preferences.
