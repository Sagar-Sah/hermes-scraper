"""Asset downloads retain source URLs and explicit failure records."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from .transport import RunInterrupted


def atomic_write(target, data):
    temp = target.with_name(target.name + ".tmp")
    with temp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, target)


async def download_images(urls, transport, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    items = []
    observations = []
    for url in sorted(urls):
        await transport.check()
        try:
            response = await transport.fetch(url, context={"lane": "image"})
            if not isinstance(response.data, bytes):
                raise ValueError("Source did not return an image content type")
            data = response.data
            extension = (
                ".png"
                if data.startswith(b"\x89PNG")
                else (
                    ".jpg"
                    if data.startswith(b"\xff\xd8")
                    else (
                        ".webp"
                        if data[8:12] == b"WEBP"
                        else ".gif" if data.startswith(b"GIF8") else ".bin"
                    )
                )
            )
            target = destination / (hashlib.sha256(data).hexdigest() + extension)
            await asyncio.to_thread(atomic_write, target, data)
            items.append(
                {
                    "name": "Image / " + target.name,
                    "path": str(target),
                    "format": extension[1:],
                }
            )
            observations.append(
                {
                    "url": url,
                    "file": target.name,
                    "status": "downloaded",
                    "evidence_id": response.response_id,
                }
            )
        except RunInterrupted:
            raise
        except Exception as exc:
            observations.append(
                {
                    "url": url,
                    "status": "unavailable",
                    "reason": "Image could not be downloaded",
                    "code": getattr(exc, "code", "image_error"),
                }
            )
    manifest = destination / "images.json"
    await asyncio.to_thread(
        atomic_write, manifest, json.dumps(observations, indent=2).encode()
    )
    items.append({"name": "Image manifest", "path": str(manifest), "format": "json"})
    return items
