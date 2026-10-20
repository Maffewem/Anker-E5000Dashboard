"""The dashboard's JSON API, one module per area."""

from . import auth, compare, control, data, settings, system, tariff, trends

ROUTERS = [module.router for module in (auth, system, data, tariff, trends, control, compare, settings)]
