"""Helpers shared by the web routes and the background loops."""

from __future__ import annotations

import logging
from .context import state




from .config import ConnectionStore
from .octopus import ECONOMY7_NIGHT, Octopus, validate as validate_octopus
from .storage import Storage
from .tariff import Tariff


log = logging.getLogger("solarbank.web")


def make_octopus(store: ConnectionStore, storage: Storage, api_key: str | None = None,
                  account: str | None = None) -> Octopus:
    config_error = None
    if api_key is None:
        api_key, account = store.load_octopus()
        if api_key and store.octopus_from_env():
            try:
                api_key, account = validate_octopus(api_key.strip("'\" "), (account or "").strip("'\" "))
            except ValueError as err:
                config_error = f"Check OCTOPUS_API_KEY and OCTOPUS_ACCOUNT in the container settings. {err}"
                log.warning("Octopus: %s", config_error)
                api_key = ""

    def offpeak() -> set[int]:
        rates = Tariff.from_dict(store.load_tariff()).slot_rates()
        return {i for i, r in enumerate(rates) if r == min(rates)} if len(set(rates)) > 1 else set(ECONOMY7_NIGHT)

    octopus = Octopus(storage, api_key or "", account or "", offpeak=offpeak)
    octopus.config_error = config_error
    return octopus


def supplier(typed: Tariff | None = None) -> str:
    """Who you buy electricity from: as picked, else Octopus if it's connected."""
    typed = typed or Tariff.from_dict(state.connection_store.load_tariff())
    return typed.supplier or ("octopus" if state.octopus.configured else "other")


def octopus_prices(typed: Tariff) -> bool:
    """Whether Octopus's prices stand in for the typed ones."""
    return state.octopus.configured and supplier(typed) == "octopus" and not typed.use_manual
