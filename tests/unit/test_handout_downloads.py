from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from recommender.ingestion.handouts import (
    HANDOUT_PATH_PREFIX,
    HANDOUT_HOST,
    HandoutEntry,
    HandoutScrapeError,
    _AllowedRedirectHandler,
    _download,
    download_handouts,
)


VALID_URL = f"https://{HANDOUT_HOST}{HANDOUT_PATH_PREFIX}TEST_F111_1.pdf"
VALID_PDF = b"%PDF-1.7\nvalid enough for mocked pdfinfo\n"


class _Response:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class _Opener:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.open = Mock(side_effect=lambda *args, **kwargs: _Response(response))


class HandoutDownloadTests(unittest.TestCase):
    def _entry(self, url: str = VALID_URL) -> HandoutEntry:
        return HandoutEntry(1, "F", "TEST 111", "Test Course", "today", url)

    def _write_index(self, path: Path) -> None:
        path.write_text(
            "<table><tr><td>1</td><td>F</td><td>TEST 111</td><td>Test Course</td><td>today</td>"
            f'<td><a href="{VALID_URL}">PDF</a></td></tr></table>',
            encoding="utf-8",
        )

    def _manifest(self, url: str, filename: str, data: bytes) -> dict:
        return {"records": [{
            "url": url, "filename": filename, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "status": "downloaded",
        }]}

    def test_altered_existing_pdf_with_manifest_mismatch_retries_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index, output, manifest = root / "index.html", root / "pdfs", root / "manifest.json"
            self._write_index(index)
            output.mkdir()
            destination = output / Path(VALID_URL).name
            destination.write_bytes(b"%PDF-old but modified")
            manifest.write_text(json.dumps(self._manifest(VALID_URL, destination.name, VALID_PDF)), encoding="utf-8")
            opener = _Opener(VALID_PDF)
            with patch("recommender.ingestion.handouts.subprocess.run", return_value=SimpleNamespace(returncode=0)), \
                 patch("recommender.ingestion.handouts.urllib.request.build_opener", return_value=opener):
                result = download_handouts(index, output, manifest, delay_seconds=0)
            self.assertEqual(result["records"][0]["status"], "downloaded")
            self.assertEqual(destination.read_bytes(), VALID_PDF)
            opener.open.assert_called_once()

    def test_manifest_matching_structurally_valid_file_skips_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index, output, manifest = root / "index.html", root / "pdfs", root / "manifest.json"
            self._write_index(index)
            output.mkdir()
            destination = output / Path(VALID_URL).name
            destination.write_bytes(VALID_PDF)
            manifest.write_text(json.dumps(self._manifest(VALID_URL, destination.name, VALID_PDF)), encoding="utf-8")
            with patch("recommender.ingestion.handouts.subprocess.run", return_value=SimpleNamespace(returncode=0)), \
                 patch("recommender.ingestion.handouts.urllib.request.build_opener") as build_opener:
                result = download_handouts(index, output, manifest, delay_seconds=0)
            self.assertEqual(result["records"][0]["status"], "skipped_existing")
            build_opener.assert_not_called()

    def test_external_redirect_is_rejected_before_follow_up_fetch(self) -> None:
        handler = _AllowedRedirectHandler()
        req = __import__("urllib.request", fromlist=["Request"]).Request(VALID_URL)
        with self.assertRaisesRegex(HandoutScrapeError, "Rejected redirect"):
            handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/out.pdf")

    def test_invalid_download_does_not_replace_existing_pdf_and_cleans_part(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "existing.pdf"
            original = b"%PDF-original that must remain"
            destination.write_bytes(original)
            opener = _Opener(b"%PDF-truncated")
            def pdfinfo(command, **kwargs):
                return SimpleNamespace(returncode=1 if command[1].endswith(".part") else 0)
            with patch("recommender.ingestion.handouts.subprocess.run", side_effect=pdfinfo), \
                 patch("recommender.ingestion.handouts.urllib.request.build_opener", return_value=opener):
                with self.assertRaisesRegex(HandoutScrapeError, "was not a PDF"):
                    _download(self._entry(), destination, 1)
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(destination.with_suffix(".pdf.part").exists())

    def test_direct_download_rejects_credentials_ports_and_traversal_without_network(self) -> None:
        invalid_urls = (
            f"https://user@{HANDOUT_HOST}{HANDOUT_PATH_PREFIX}x.pdf",
            f"https://{HANDOUT_HOST}:444{HANDOUT_PATH_PREFIX}x.pdf",
            f"https://{HANDOUT_HOST}{HANDOUT_PATH_PREFIX}%2e%2e/secret.pdf",
        )
        for url in invalid_urls:
            with self.subTest(url=url), patch("recommender.ingestion.handouts.urllib.request.build_opener") as build_opener:
                with self.assertRaisesRegex(HandoutScrapeError, "Rejected URL"):
                    _download(self._entry(url), Path("unused.pdf"), 1)
                build_opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
