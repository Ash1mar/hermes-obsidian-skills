"""Small immutable internal views; public mutable proofs remain detached."""
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True,slots=True)
class FrozenOverlay(Mapping):
    base: Mapping
    key: object
    value: object

    def __getitem__(self,key):
        return self.value if key==self.key else self.base[key]

    def __iter__(self):
        yield from self.base
        if self.key not in self.base:yield self.key

    def __len__(self):return len(self.base)+int(self.key not in self.base)

    def __deepcopy__(self,memo):return self


@dataclass(frozen=True,slots=True)
class FrozenProof:
    files: Mapping
    directories: Mapping
    schema_sha256: str

    def detached(self):
        # The original interface permits mutation, including directory lists.
        # Strings/bytes are immutable; no recursive deep copy is necessary.
        return {'files':dict(self.files),
            'directories':{name:list(members) for name,members in self.directories.items()},
            'schema_sha256':self.schema_sha256}

    def __deepcopy__(self,memo):return self
