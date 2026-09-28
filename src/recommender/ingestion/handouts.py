from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO


HANDOUT_HOST = "academic.bits-pilani.ac.in"
HANDOUT_PATH_PREFIX = "/Faculty/Course_Handouts/Handout_Files/Current_Handouts/"


class HandoutScrapeError(RuntimeError):
    pass


@dataclass(frozen=True)
class HandoutEntry:
    serial_number: int
    component_code: str
    course_code: str
    course_title: str
    uploaded_at: str
    url: str

    @property
    def filename(self) -> str:
        return Path(urllib.parse.urlparse(self.url).path).name


class _HandoutTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[HandoutEntry] = []
        self._in_row = False
        self._in_cell = False
        self._cell_text: list[str] = []
        self._cells: list[str] = []
        self._handout_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._handout_url = None
        elif tag == "td" and self._in_row:
            self._in_cell = True
            self._cell_text = []
        elif tag == "a" and self._in_cell:
            href = dict(attrs).get("href")
            if href and _is_allowed_handout_url(href):
                self._handout_url = href

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._in_cell:
            self._cells.append(" ".join("".join(self._cell_text).split()))
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            self._finish_row()
            self._in_row = False

    def _finish_row(self) -> None:
        if self._handout_url is None or len(self._cells) < 5:
            return
        try:
            serial_number = int(self._cells[0])
        except ValueError:
            return
        self.entries.append(
            HandoutEntry(
                serial_number=serial_number,
                component_code=self._cells[1],
                course_code=self._cells[2],
                course_title=self._cells[3],
                uploaded_at=self._cells[4],
                url=self._handout_url,
            )
        )


def _is_allowed_handout_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme.casefold() != "https" or parsed.hostname is None
                or parsed.hostname.casefold() != HANDOUT_HOST or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment):
            return False
        decoded_path = parsed.path
        for _ in range(10):
            decoded = urllib.parse.unquote(decoded_path)
            if decoded == decoded_path:
                break
            decoded_path = decoded
        else:
            return False
        segments = decoded_path.split("/")
        return (
            
            
            parsed.path.casefold().startswith(HANDOUT_PATH_PREFIX.casefold())
            and decoded_path.casefold().startswith(HANDOUT_PATH_PREFIX.casefold())
            and decoded_path.casefold().endswith(".pdf")
            and "\\" not in decoded_path
            and not any(segment in {".", ".."} for segment in segments)
            and not any(ord(char) < 32 or char.isspace() for char in url)
        )
    except (ValueError, UnicodeError):
        return False


def parse_handout_index(path: str | Path) -> tuple[HandoutEntry, ...]:
    source = Path(path)
    if not source.is_file():
        raise HandoutScrapeError(f"Saved handout index not found: {source}")
    parser = _HandoutTableParser()
    try:
        parser.feed(source.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise HandoutScrapeError(f"Saved handout index is not UTF-8: {source}") from exc
    entries = sorted(parser.entries, key=lambda item: item.serial_number)
    if not entries:
        raise HandoutScrapeError("No allowed handout PDF links were found in the saved index.")
    urls = [entry.url for entry in entries]
    if len(urls) != len(set(urls)):
        raise HandoutScrapeError("The saved index contains duplicate handout URLs.")
    filenames = [entry.filename for entry in entries]
    if len(filenames) != len(set(filenames)):
        raise HandoutScrapeError("The saved index contains duplicate destination filenames.")
    return tuple(entries)


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _valid_pdf(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 5:
        return False
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            return False
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, check=False, timeout=60,
        )
    except FileNotFoundError as exc:
        raise HandoutScrapeError("Cannot validate PDF structure: pdfinfo is unavailable; install Poppler utilities.") from exc
    except OSError as exc:
        raise HandoutScrapeError(f"Cannot validate PDF structure with pdfinfo: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HandoutScrapeError("PDF structural validation timed out after 60 seconds.") from exc
    return result.returncode == 0


class _AllowedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_allowed_handout_url(newurl):
            raise HandoutScrapeError(f"Rejected redirect to a URL outside the approved handout location: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _write_manifest(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _download(entry: HandoutEntry, destination: Path, timeout: float) -> tuple[int, str]:
    if not _is_allowed_handout_url(entry.url):
        raise HandoutScrapeError(f"Rejected URL outside the approved handout location: {entry.url}")
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        entry.url,
        headers={"User-Agent": "BITS-Academic-Recommender/0.1 (course-handout archival)"},
    )
    try:
        opener = urllib.request.build_opener(_AllowedRedirectHandler())
        with opener.open(request, timeout=timeout) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if not _valid_pdf(temporary):
            raise HandoutScrapeError(f"Response for {entry.course_code} was not a PDF")
        size = temporary.stat().st_size
        with temporary.open("rb") as stream:
            digest = _sha256_stream(stream)
        os.replace(temporary, destination)
        return size, digest
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def download_handouts(
    index_path: str | Path,
    output_directory: str | Path,
    manifest_path: str | Path,
    *,
    delay_seconds: float = 0.25,
    timeout_seconds: float = 60,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    if delay_seconds < 0:
        raise HandoutScrapeError("Download delay cannot be negative.")
    if timeout_seconds <= 0:
        raise HandoutScrapeError("Download timeout must be positive.")
    entries = parse_handout_index(index_path)
    if limit is not None:
        if limit < 1:
            raise HandoutScrapeError("Download limit must be at least 1.")
        entries = entries[:limit]

    destination_root = Path(output_directory)
    destination_root.mkdir(parents=True, exist_ok=True)
    manifest_file = Path(manifest_path)
    records: list[dict] = []
    previous_records: dict[str, dict] = {}
    if manifest_file.is_file():
        try:
            previous = json.loads(manifest_file.read_text(encoding="utf-8"))
            previous_records = {
                record["url"]: record for record in previous.get("records", [])
                if isinstance(record, dict) and isinstance(record.get("url"), str)
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
            previous_records = {}
    manifest = {
        "schema_version": "bits-handout-download-v1",
        "source_index": str(index_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_discovered": len(parse_handout_index(index_path)),
        "selected": len(entries),
        "records": records,
    }

    for position, entry in enumerate(entries, start=1):
        destination = destination_root / entry.filename
        record = {**asdict(entry), "filename": entry.filename}
        try:
            existing_valid = _valid_pdf(destination)
            prior = previous_records.get(entry.url)
            if existing_valid:
                with destination.open("rb") as stream:
                    digest = _sha256_stream(stream)
                size = destination.stat().st_size
                manifest_matches = (
                    prior is None or (
                        prior.get("filename") == entry.filename
                        and prior.get("size_bytes") == size
                        and prior.get("sha256") == digest
                    )
                )
            else:
                manifest_matches = False
            if existing_valid and manifest_matches:
                record.update(status="skipped_existing", size_bytes=size, sha256=digest)
            elif dry_run:
                record.update(status="planned")
            else:
                size, digest = _download(entry, destination, timeout_seconds)
                record.update(status="downloaded", size_bytes=size, sha256=digest)
        except (OSError, urllib.error.URLError, HandoutScrapeError) as exc:
            record.update(status="failed", error=str(exc))
        records.append(record)
        _write_manifest(manifest, manifest_file)
        if position == 1 or position % 25 == 0 or position == len(entries):
            print(f"handouts: {position}/{len(entries)} ({record['status']})", flush=True)
        if not dry_run and position != len(entries):
            time.sleep(delay_seconds)

    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["summary"] = {
        status: sum(record["status"] == status for record in records)
        for status in ("downloaded", "skipped_existing", "planned", "failed")
    }
    _write_manifest(manifest, manifest_file)
    return manifest
