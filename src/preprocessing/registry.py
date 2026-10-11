"""Explicit preprocessing registry; configuration never imports arbitrary code."""

from __future__ import annotations

from collections.abc import Callable

from src.preprocessing.burn_severity import optical, optical_case, optical_no_aux, optical_scl, optical_scl_no_aux


PREPROCESSORS: dict[str, dict[str, tuple[Callable, frozenset[str]]]] = {
    "burn_severity": {
        "optical_no_aux": (optical_no_aux, frozenset({"dnbr", "valid"})),
        "optical_scl_no_aux": (optical_scl_no_aux, frozenset({"dnbr", "valid"})),
        "optical": (optical, frozenset({"dnbr", "valid", "landcover"})),
        "optical_scl": (optical_scl, frozenset({"dnbr", "valid", "landcover"})),
        "optical_case": (optical_case, frozenset({"dnbr", "valid", "landcover"})),
    },
}
