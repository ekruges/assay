from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS_BULK_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
)
SUBMISSIONS_BULK_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
)


class SecError(RuntimeError):
    """A clear SEC input, network, or response failure."""


@dataclass(frozen=True)
class Company:
    ticker: str
    cik: int
    name: str


class SecClient:
    def __init__(
        self,
        cache_dir: str | Path = ".cache/sec",
        user_agent: str | None = None,
        refresh: bool = False,
        requests_per_second: float = 8,
    ) -> None:
        if not 0 < requests_per_second < 10:
            raise ValueError("SEC request rate must be positive and below 10 per second")
        self.cache_dir = Path(cache_dir)
        self.user_agent = user_agent or os.environ.get("ASSAY_SEC_USER_AGENT")
        self.refresh = refresh
        self.min_request_interval = 1 / requests_per_second
        self._last_request_at = 0.0

    def company_tickers(self) -> dict[str, Any]:
        return self._get_json(TICKERS_URL, "company_tickers.json")

    def companyfacts(self, cik: int) -> dict[str, Any]:
        return self._get_json(
            COMPANYFACTS_URL.format(cik=cik), f"CIK{cik:010d}-companyfacts.json"
        )

    def submissions(self, cik: int) -> dict[str, Any]:
        return self._get_json(
            SUBMISSIONS_URL.format(cik=cik), f"CIK{cik:010d}-submissions.json"
        )

    def bulk_archives(self) -> tuple[Path, Path]:
        """Download or reuse the SEC's nightly all-filer archives."""
        return (
            self._download(
                COMPANYFACTS_BULK_URL,
                self.cache_dir / "bulk" / "companyfacts.zip",
            ),
            self._download(
                SUBMISSIONS_BULK_URL,
                self.cache_dir / "bulk" / "submissions.zip",
            ),
        )

    def resolve_ticker(self, ticker: str) -> Company:
        wanted = ticker.strip().upper()
        if not wanted:
            raise SecError("ticker is empty")

        for row in self.company_tickers().values():
            if str(row.get("ticker", "")).upper() == wanted:
                return Company(
                    ticker=wanted,
                    cik=int(row["cik_str"]),
                    name=str(row.get("title", wanted)),
                )
        raise SecError(f"ticker {wanted!r} was not found in the SEC ticker file")

    def _get_json(self, url: str, cache_name: str) -> dict[str, Any]:
        path = self.cache_dir / cache_name
        if path.exists() and not self.refresh:
            return _read_json(path)
        self._require_identity()

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Host": urllib.parse.urlsplit(url).netloc,
            },
        )
        try:
            with self._open(request) as response:
                payload = response.read()
                encoding = response.headers.get("Content-Encoding", "").lower()
        except urllib.error.HTTPError as exc:
            raise SecError(f"SEC returned HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            raise SecError(f"could not reach SEC for {url}: {exc.reason}") from exc

        if encoding == "gzip":
            import gzip

            payload = gzip.decompress(payload)
        elif encoding == "deflate":
            import zlib

            payload = zlib.decompress(payload)

        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecError(f"SEC returned invalid JSON for {url}") from exc
        if not isinstance(data, dict):
            raise SecError(f"SEC returned a non-object JSON payload for {url}")

        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)
        return data

    def _download(self, url: str, path: Path) -> Path:
        if path.exists() and not self.refresh:
            return path
        self._require_identity()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent or "",
                "Accept-Encoding": "identity",
                "Host": urllib.parse.urlsplit(url).netloc,
            },
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with self._open(request, timeout=120) as response:
                with tempfile.NamedTemporaryFile(
                    "wb", dir=path.parent, delete=False
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    while chunk := response.read(1024 * 1024):
                        tmp.write(chunk)
            if not tmp_path or not zipfile.is_zipfile(tmp_path):
                raise SecError(f"SEC returned an invalid ZIP archive for {url}")
            tmp_path.replace(path)
            return path
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()

    def _open(
        self, request: urllib.request.Request, timeout: int = 30
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(4):
            self._throttle()
            try:
                return urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise SecError(
                        f"SEC returned HTTP {exc.code} for {request.full_url}"
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2**attempt)
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 3:
                    raise SecError(
                        f"could not reach SEC for {request.full_url}: {exc.reason}"
                    ) from exc
                time.sleep(2**attempt)
        raise SecError(f"could not reach SEC for {request.full_url}: {last_error}")

    def _throttle(self) -> None:
        wait = self.min_request_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _require_identity(self) -> None:
        email = (
            re.search(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b", self.user_agent)
            if self.user_agent
            else None
        )
        placeholder_domains = ("@example.com", "@example.org", "@example.net")
        if not email or email.group(0).lower().endswith(placeholder_domains):
            raise SecError(
                "live SEC requests require --user-agent or ASSAY_SEC_USER_AGENT "
                "with a real contact email"
            )


class BulkSecStore:
    """Random access to the SEC's nightly all-filer ZIP archives."""

    def __init__(self, companyfacts_zip: str | Path, submissions_zip: str | Path):
        try:
            self.companyfacts_archive = zipfile.ZipFile(companyfacts_zip)
            try:
                self.submissions_archive = zipfile.ZipFile(submissions_zip)
            except Exception:
                self.companyfacts_archive.close()
                raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise SecError(f"could not open SEC bulk archives: {exc}") from exc

    def companyfacts(self, cik: int) -> dict[str, Any]:
        return self._read(self.companyfacts_archive, cik, "companyfacts")

    def submissions(self, cik: int) -> dict[str, Any]:
        return self._read(self.submissions_archive, cik, "submissions")

    def has_companyfacts(self, cik: int) -> bool:
        return f"CIK{cik:010d}.json" in self.companyfacts_archive.NameToInfo

    def close(self) -> None:
        self.companyfacts_archive.close()
        self.submissions_archive.close()

    def __enter__(self) -> BulkSecStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _read(
        archive: zipfile.ZipFile, cik: int, archive_name: str
    ) -> dict[str, Any]:
        name = f"CIK{cik:010d}.json"
        try:
            payload = archive.read(name)
        except KeyError as exc:
            raise SecError(f"CIK {cik:010d} is absent from {archive_name}.zip") from exc
        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecError(f"invalid JSON for CIK {cik:010d} in {archive_name}.zip") from exc
        if not isinstance(data, dict):
            raise SecError(
                f"non-object JSON for CIK {cik:010d} in {archive_name}.zip"
            )
        return data


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SecError(f"could not read cached JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SecError(f"cached JSON {path} is not an object")
    return data
