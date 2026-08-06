from pathlib import Path
import hashlib
import json


class FingerprintStore:

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, str] = {}
        self.load()

    def load(self):
        if self.path.exists():
            try:
                self.data = json.loads(
                    self.path.read_text(
                        encoding="utf-8"
                    )
                )
            except Exception:
                self.data = {}

    def save(self):
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.path.write_text(
            json.dumps(
                self.data,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

    @staticmethod
    def fingerprint(file: Path) -> str:
        digest = hashlib.sha256()

        with open(file, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)

        return digest.hexdigest()

    def has_changed(self, source_id: str, file: Path) -> bool:
        return self.data.get(source_id) != self.fingerprint(file)

    def update(self, source_id: str, file: Path):
        self.data[source_id] = self.fingerprint(file)

    def remove(self, source_id: str):
        self.data.pop(source_id, None)

    def all_files(self):
        return set(self.data.keys())
