from __future__ import annotations

from pathlib import Path


def cached_model_path(
    model_name: str,
    revision: str | None,
    *,
    cache_dir: str | None = None,
    default_namespace: str | None = None,
) -> str:
    """Resolve model files without passing a Hub repository ID to the loader."""
    path = Path(model_name).expanduser()
    if path.is_dir():
        return str(path.resolve())

    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    repositories = [model_name]
    if default_namespace and "/" not in model_name:
        repositories.insert(0, f"{default_namespace}/{model_name}")
    for repo_id in repositories:
        try:
            return snapshot_download(repo_id, revision=revision, cache_dir=cache_dir, local_files_only=True)
        except LocalEntryNotFoundError as exc:
            missing = exc
    raise RuntimeError(f"Local model snapshot is missing: {model_name!r} at revision {revision or 'main'!r}") from missing
