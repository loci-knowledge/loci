from .aspect_suggest import suggest_aspects_sync
from .folder_suggest import suggest_folders
from .ingest import CaptureResult, ingest_file, ingest_text, ingest_url

__all__ = [
    "ingest_url",
    "ingest_file",
    "ingest_text",
    "CaptureResult",
    "suggest_folders",
    "suggest_aspects_sync",
]
