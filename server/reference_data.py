"""Read-only reference-data boundary. No database or network is opened here.

The site adapter supplies one coherent, public-data snapshot. It must use
deployment-owned read-only views; model input never selects tables or SQL.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol


class ReferenceDataUnavailable(RuntimeError):
    """The configured read-only source cannot supply a coherent snapshot."""


@dataclass(frozen=True)
class ReferenceSnapshot:
    snapshot_id: str
    as_of: str
    sources: list[dict] = field(default_factory=list)
    bonds: list[dict] = field(default_factory=list)
    issuers: list[dict] = field(default_factory=list)
    terms: dict[str, dict] = field(default_factory=dict)

    def lookup_bonds(self, bond_id: str) -> list[dict]:
        return deepcopy([row for row in self.bonds if row.get('bond_id') == bond_id])

    def search_bonds(self) -> list[dict]:
        """Full authorized dictionary, before filtering and pagination."""
        return deepcopy(self.bonds)

    def lookup_issuers(self, issuer_id: str) -> list[dict]:
        return deepcopy([row for row in self.issuers if row.get('issuer_id') == issuer_id])

    def search_issuers(self) -> list[dict]:
        return deepcopy(self.issuers)

    def load_bond_terms(self, bond_id: str, terms_snapshot_id: str) -> dict | None:
        value = self.terms.get(bond_id)
        if value is None or value.get('terms_snapshot_id') != terms_snapshot_id:
            return None
        return deepcopy(value)


class ReferenceDataProvider(Protocol):
    def snapshot(self) -> ReferenceSnapshot: ...


class UnconfiguredReferenceData:
    def snapshot(self) -> ReferenceSnapshot:
        raise ReferenceDataUnavailable('NOT_CONFIGURED')
