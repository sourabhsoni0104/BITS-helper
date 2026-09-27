from __future__ import annotations

import argparse
import json
from pathlib import Path

from recommender.ingestion.corpus import CorpusError, build_corpus, search_corpus
from recommender.ingestion.documents import DOCUMENT_TYPES, ExtractionError, extract_document, inventory_sources, write_json_atomic
from recommender.ingestion.handouts import HandoutScrapeError, download_handouts
from recommender.ingestion.marksheets import MarksheetParseError, parse_marksheet
from recommender.ingestion.snapshots import SnapshotError, file_sha256, load_snapshot, publish_snapshot, validate_snapshot
from recommender.ingestion.review import build_review_bundle, publish_review
from recommender.services.document_answers import answer_question


def _data_path(value: str) -> Path:
    path = Path(value)
    return path / "active.json" if path.is_dir() else path


def main() -> None:
    parser = argparse.ArgumentParser(prog="bits-recommender")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="Validate and atomically publish a normalized JSON snapshot")
    ingest.add_argument("--input", required=True, help="Candidate normalized JSON snapshot")
    ingest.add_argument("--output", default="data/processed/active.json")
    validate = sub.add_parser("validate", help="Validate a normalized snapshot")
    validate.add_argument("--data", default="data/processed/active.json")
    status = sub.add_parser("status", help="Show active dataset status")
    status.add_argument("--data", default="data/processed/active.json")
    inventory = sub.add_parser("inventory", help="Inventory candidate academic source files without trusting filename classification")
    inventory.add_argument("--input", default="data/raw")
    inventory.add_argument("--report", default="data/reports/source-inventory.json")
    extract = sub.add_parser("extract", help="Extract page-aware text from one confirmed source document")
    extract.add_argument("--input", required=True)
    extract.add_argument("--type", required=True, choices=DOCUMENT_TYPES)
    extract.add_argument("--output", required=True)
    extract.add_argument("--campus")
    extract.add_argument("--programme")
    extract.add_argument("--batch")
    extract.add_argument("--semester")
    handouts = sub.add_parser("download-handouts", help="Download handout PDFs from a browser-saved authenticated index")
    handouts.add_argument("--index", required=True, help="Saved All Courses Handouts HTML page")
    handouts.add_argument("--output", default="data/raw/handouts")
    handouts.add_argument("--manifest", default="data/reports/handout-downloads.json")
    handouts.add_argument("--delay", type=float, default=0.25, help="Delay between requests in seconds")
    handouts.add_argument("--timeout", type=float, default=60)
    handouts.add_argument("--limit", type=int)
    handouts.add_argument("--dry-run", action="store_true")
    corpus = sub.add_parser("build-corpus", help="Incrementally index PDF and DOCX reference documents")
    corpus.add_argument("--input", default="data")
    corpus.add_argument("--output", default="data/processed/document-corpus.sqlite")
    search = sub.add_parser("search-docs", help="Retrieve cited pages from the indexed PDF corpus")
    search.add_argument("--data", default="data/processed/document-corpus.sqlite")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--type", dest="document_type")
    search.add_argument("--semester")
    answer = sub.add_parser("answer-docs", help="Answer from cited source passages")
    answer.add_argument("--data", default="data/processed/document-corpus.sqlite")
    answer.add_argument("--query", required=True)
    answer.add_argument("--type", dest="document_type")
    answer.add_argument("--semester")
    draft = sub.add_parser("prepare-review", help="Extract source-linked candidates for explicit review")
    draft.add_argument("--corpus", default="data/processed/document-corpus.sqlite")
    draft.add_argument("--index", default="data/raw/All Courses Handouts.html")
    draft.add_argument("--output", default="data/processed/review-bundle.json")
    draft.add_argument("--campus", default="Pilani")
    draft.add_argument("--semester", default="2026-T1")
    draft.add_argument("--admission-year", type=int, default=2025)
    reviewed = sub.add_parser("publish-review", help="Validate and publish an explicitly reviewed candidate")
    reviewed.add_argument("--input", default="data/processed/review-bundle.json")
    reviewed.add_argument("--output", default="data/processed/active.json")
    reviewed.add_argument("--reviewer", required=True)
    marksheet = sub.add_parser("parse-marksheet", help="Extract reviewable course-attempt candidates from a marksheet")
    marksheet.add_argument("--input", required=True)
    marksheet.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "inventory":
            report = inventory_sources(args.input)
            write_json_atomic(report, args.report)
            print(json.dumps({"report": args.report, "files": len(report["files"]), "classes_without_candidates": report["classes_without_candidates"]}, indent=2))
            return
        if args.command == "extract":
            document = extract_document(
                args.input,
                args.type,
                campus_scope=args.campus,
                programme_scope=args.programme,
                batch_scope=args.batch,
                semester_scope=args.semester,
            )
            write_json_atomic(document, args.output)
            print(json.dumps({"output": args.output, "document": document["document"], "pages": len(document["pages"]), "issues": len(document["issues"])}, indent=2))
            return
        if args.command == "download-handouts":
            manifest = download_handouts(
                args.index,
                args.output,
                args.manifest,
                delay_seconds=args.delay,
                timeout_seconds=args.timeout,
                limit=args.limit,
                dry_run=args.dry_run,
            )
            print(json.dumps({"manifest": args.manifest, "discovered": manifest["total_discovered"], **manifest["summary"]}, indent=2))
            if manifest["summary"]["failed"]:
                parser.exit(2, "error: one or more handout downloads failed; rerun to retry\n")
            return
        if args.command == "build-corpus":
            print(json.dumps(build_corpus(args.input, args.output), indent=2))
            return
        if args.command == "search-docs":
            print(json.dumps(search_corpus(args.data, args.query, args.limit, document_type=args.document_type, semester=args.semester), indent=2))
            return
        if args.command == "answer-docs":
            print(json.dumps(answer_question(args.data, args.query, document_type=args.document_type, semester=args.semester), indent=2))
            return
        if args.command == "prepare-review":
            bundle = build_review_bundle(args.corpus, args.index, args.output, campus=args.campus,
                                         semester=args.semester, admission_year=args.admission_year)
            print(json.dumps({'output': args.output, 'status': bundle['status'],
                **{key: len(bundle['snapshot'][key]) for key in ('courses','offerings','evidence','policy_contexts')},
                'notes': bundle['notes']}, indent=2))
            return
        if args.command == "publish-review":
            print(json.dumps(publish_review(args.input, args.output, args.reviewer), indent=2))
            return
        if args.command == "parse-marksheet":
            result = parse_marksheet(args.input)
            write_json_atomic(result, args.output)
            print(json.dumps({
                "output": args.output,
                "report_type": result["report_type"],
                "profile_import_status": result["profile_import_status"],
                "attempt_candidates": len(result["attempt_candidates"]),
                "pending_course_candidates": len(result["pending_course_candidates"]),
                "requirement_progress_candidates": len(result["requirement_progress_candidates"]),
                "issues": len(result["issues"]),
            }, indent=2))
            return
        if args.command == "ingest":
            snapshot = publish_snapshot(args.input, args.output)
            print(json.dumps({"published": args.output, "dataset_version": snapshot.dataset_version, "sha256": file_sha256(args.output)}, indent=2))
            return
        path = _data_path(args.data)
        snapshot = load_snapshot(path)
        issues = validate_snapshot(snapshot)
        if args.command == "validate":
            print(json.dumps({"valid": not any(item.severity == "error" for item in issues), "issues": [item.__dict__ for item in issues]}, indent=2))
        else:
            print(json.dumps({
                "dataset_version": snapshot.dataset_version,
                "synthetic": snapshot.synthetic,
                "documents": len(snapshot.documents),
                "courses": len(snapshot.courses),
                "offerings": len(snapshot.offerings),
                "policy_contexts": len(snapshot.policy_contexts),
                "issues": [item.__dict__ for item in issues],
            }, indent=2))
    except (SnapshotError, ExtractionError, HandoutScrapeError, CorpusError, MarksheetParseError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
