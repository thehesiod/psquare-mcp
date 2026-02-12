from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from parentsquare_mcp.client import PSClient


def download_file(client: PSClient, url: str, download_dir: Path, filename: str | None = None) -> Path:
    """Download a file from URL to local disk.

    Returns the saved file path.
    """
    download_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        parsed = urlparse(url)
        filename = Path(parsed.path).name or "download"

    dest = download_dir / filename

    # Handle name conflicts
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = download_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    resp = client.get_raw(url, stream=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    return dest
