from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .models import RadarFrame


CAP_FILENAME_PATTERN = re.compile(
    r"^JAR(?P<timestamp>\d{12})\.(?P<product>CAP[^.]*)\.png$", re.IGNORECASE
)


class SourceProtocolError(RuntimeError):
    """Raised when the public source does not match the observed schema."""


@dataclass(frozen=True)
class ManifestSnapshot:
    payload: dict[str, Any]
    frames: tuple[RadarFrame, ...]
    fetched_at: datetime
    etag: str | None
    last_modified: str | None


def _filename_from_url(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", maxsplit=1)[-1])


def parse_cappi_frames(payload: dict[str, Any], source_timezone) -> tuple[RadarFrame, ...]:
    """Parse only CAPPI products and derive precise timestamps from filenames."""

    image_list = payload.get("img_list")
    if not isinstance(image_list, dict):
        raise SourceProtocolError("manifest has no img_list object")

    frames: list[RadarFrame] = []
    for item in image_list.values():
        if not isinstance(item, dict):
            continue
        url = item.get("wh_img")
        display_timestamp = item.get("timp")
        if not isinstance(url, str) or not isinstance(display_timestamp, str):
            continue

        filename = _filename_from_url(url)
        match = CAP_FILENAME_PATTERN.fullmatch(filename)
        if match is None:
            continue

        observed_at = datetime.strptime(match.group("timestamp"), "%y%m%d%H%M%S")
        frames.append(
            RadarFrame(
                filename=filename,
                url=url,
                observed_at=observed_at.replace(tzinfo=source_timezone),
                display_timestamp=display_timestamp,
            )
        )

    unique_by_filename = {frame.filename: frame for frame in frames}
    return tuple(sorted(unique_by_filename.values(), key=lambda frame: (frame.observed_at, frame.filename)))


class YarlovoClient:
    """Small synchronous client for the manifest and its direct PNG frame URLs."""

    def __init__(self, manifest_url: str, source_timezone, client: httpx.Client | None = None):
        self.manifest_url = manifest_url
        self.source_timezone = source_timezone
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "YarlovoClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_manifest(self) -> ManifestSnapshot:
        # The viewer itself uses a changing query string. Use the same cache-bust
        # behaviour while preserving any source URL query parameters.
        response = self._client.get(self.manifest_url, params={"ts": int(time.time())})
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise SourceProtocolError("manifest response is not valid JSON") from error
        if not isinstance(payload, dict):
            raise SourceProtocolError("manifest root must be a JSON object")

        return ManifestSnapshot(
            payload=payload,
            frames=parse_cappi_frames(payload, self.source_timezone),
            fetched_at=datetime.now(timezone.utc),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    def download_frame(self, frame: RadarFrame) -> bytes:
        response = self._client.get(frame.url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not content_type.lower().startswith("image/png"):
            raise SourceProtocolError(
                f"{frame.filename} is not a PNG response (Content-Type: {content_type!r})"
            )
        return response.content
