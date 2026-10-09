"""Octopus Energy: the account's tariffs, their half-hourly prices, and
Intelligent Octopus Go dispatch slots.

Read-only. Prices come from Octopus's public REST API once the account's
tariff is known; the account lookup and dispatch slots need the customer's
API key (octopus.energy > Account > Personal details > API access).

Prices are kept per local half hour in the dashboard's database so the
battery payback can use what was actually charged, including Agile's daily
changes and earlier tariffs the account was on.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .runtime import battery_size
from .tariff import slot_time

log = logging.getLogger("solarbank.octopus")

# Octopus answers on both; the second is the newer Kraken host.
API_HOSTS = ("https://api.octopus.energy", "https://api.oegb-kraken.energy")
ACCOUNT_RE = re.compile(r"^A-[0-9A-Z]{6,10}$")
KEY_RE = re.compile(r"^sk_live_[A-Za-z0-9]{8,64}$")
TARIFF_RE = re.compile(r"^(?P<energy>[EG])-(?P<rates>[0-9]R)-(?P<product>[A-Z0-9-]+)-(?P<region>[A-P])$")

# Product code patterns, checked in order, for a friendly kind.
KINDS = (
    ("INTELLI", "intelligent_go"),
    ("AGILE-OUTGOING", "agile_outgoing"),
    ("OUTGOING", "outgoing"),  # before GO, which it contains
    ("AGILE", "agile"),
    ("FLUX", "flux"),
    ("COSY", "cosy"),
    ("GO", "go"),
    ("SILVER", "tracker"),
    ("TRACKER", "tracker"),
)

Fetch = Callable[[str, dict, bytes | None], dict]


def _urllib_fetch(url: str, headers: dict, body: bytes | None) -> dict:
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "solarbank-dashboard", **headers})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read())


class OctopusError(Exception):
    pass


def tariff_parts(code: str) -> dict | None:
    m = TARIFF_RE.match(code or "")
    return m.groupdict() if m else None


def tariff_kind(product: str) -> str:
    for needle, kind in KINDS:
        if needle in product:
            return kind
    return "flat"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Client:
    """Thin wrapper over the REST and GraphQL APIs. Every call is blocking."""

    def __init__(self, api_key: str, account: str, fetch: Fetch | None = None) -> None:
        self.api_key = api_key
        self.account = account
        self._fetch = fetch or (lambda *args: _urllib_fetch(*args))
        self._host = API_HOSTS[0]
        self._token: str | None = None

    def _get(self, path: str, params: dict | None = None, auth: bool = False) -> dict:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        headers = {}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{self.api_key}:".encode()).decode()
        return self._call(lambda host: self._fetch(f"{host}{path}{query}", headers, None))

    def _call(self, request: Callable[[str], dict]) -> dict:
        """Try the working host first, then the other one if it can't be reached."""
        hosts = [self._host, *[h for h in API_HOSTS if h != self._host]]
        last: Exception | None = None
        for host in hosts:
            try:
                out = request(host)
                self._host = host
                return out
            except urllib.error.HTTPError as err:
                if err.code in (401, 403):
                    raise OctopusError("Octopus refused the API key. Check it under Personal details > API access.") from err
                if err.code == 404:
                    path = urllib.parse.urlparse(err.filename or "").path if isinstance(err.filename, str) else ""
                    if "/accounts/" in path:
                        raise OctopusError("Octopus doesn't recognise that account number.") from err
                    raise OctopusError(f"Octopus has nothing at {path or 'that address'} (404).") from err
                last = err
                if err.code < 500:
                    break
            except (urllib.error.URLError, OSError, ValueError) as err:
                last = err
        raise OctopusError(f"Couldn't reach Octopus: {last}")

    def _paged(self, path: str, params: dict) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            body = self._get(path, {**params, "page_size": 1500, "page": page})
            out.extend(body.get("results") or [])
            if not body.get("next") or page >= 50:
                return out
            page += 1

    # ---- REST ----

    def account_info(self) -> dict:
        return self._get(f"/v1/accounts/{self.account}/", auth=True)

    def product(self, code: str) -> dict:
        return self._get(f"/v1/products/{code}/")

    def unit_rates(self, product: str, tariff: str, start: datetime, end: datetime, which: str = "standard") -> list[dict]:
        path = f"/v1/products/{product}/electricity-tariffs/{tariff}/{which}-unit-rates/"
        return _prefer_direct_debit(self._paged(path, {"period_from": _iso(start), "period_to": _iso(end)}))

    def standing_charge(self, product: str, tariff: str) -> float | None:
        now = datetime.now(timezone.utc)
        path = f"/v1/products/{product}/electricity-tariffs/{tariff}/standing-charges/"
        rows = _prefer_direct_debit(self._paged(path, {"period_from": _iso(now - timedelta(days=1)), "period_to": _iso(now)}))
        return float(rows[0]["value_inc_vat"]) if rows else None

    # ---- GraphQL (Intelligent Octopus Go) ----

    def _graphql(self, query: str, variables: dict | None = None, retry: bool = True) -> dict:
        if not self._token:
            body = self._call(lambda host: self._fetch(
                f"{host}/v1/graphql/", {"Content-Type": "application/json"},
                json.dumps({"query": "mutation($k:String!){obtainKrakenToken(input:{APIKey:$k}){token}}",
                            "variables": {"k": self.api_key}}).encode()))
            token = ((body.get("data") or {}).get("obtainKrakenToken") or {}).get("token")
            if not token:
                raise OctopusError("Octopus refused the API key. Check it under Personal details > API access.")
            self._token = token
        token = self._token
        body = self._call(lambda host: self._fetch(
            f"{host}/v1/graphql/", {"Content-Type": "application/json", "Authorization": token},
            json.dumps({"query": query, "variables": variables or {}}).encode()))
        errors = body.get("errors") or []
        if errors and retry and any("JWT" in str(e) or "token" in str(e).lower() for e in errors):
            self._token = None  # expired; get a new one once
            return self._graphql(query, variables, retry=False)
        if errors and not body.get("data"):
            raise OctopusError(f"Octopus said: {errors[0].get('message', errors[0])}")
        return body.get("data") or {}

    def dispatches(self) -> dict:
        """Planned and completed smart-charge slots, newest API first."""
        acc = {"a": self.account}
        data = self._graphql("query($a:String!){devices(accountNumber:$a){id deviceType}"
                             " completedDispatches(accountNumber:$a){start end delta}}", acc)
        planned: list[dict] = []
        for device in data.get("devices") or []:
            try:
                d = self._graphql("query($d:String!){flexPlannedDispatches(deviceId:$d){start end type energyAddedKwh}}",
                                  {"d": device["id"]})
                planned += d.get("flexPlannedDispatches") or []
            except OctopusError:
                pass
        if not planned:
            try:
                planned = self._graphql("query($a:String!){plannedDispatches(accountNumber:$a){start end delta}}",
                                        acc).get("plannedDispatches") or []
            except OctopusError:
                pass
        return {"planned": planned, "completed": data.get("completedDispatches") or []}


def _prefer_direct_debit(rows: list[dict]) -> list[dict]:
    """Octopus lists some prices twice, once per payment method; keep direct debit."""
    if any((r.get("payment_method") or "").upper() == "DIRECT_DEBIT" for r in rows):
        rows = [r for r in rows if (r.get("payment_method") or "DIRECT_DEBIT").upper() == "DIRECT_DEBIT"]
    return rows


@dataclass(frozen=True)
class Agreement:
    tariff: str
    product: str
    valid_from: datetime | None
    valid_to: datetime | None
    export: bool

    @property
    def two_rate(self) -> bool:
        return (tariff_parts(self.tariff) or {}).get("rates") == "2R"

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return (self.valid_from is None or self.valid_from < end) and (self.valid_to is None or self.valid_to > start)


def agreements(account: dict) -> list[Agreement]:
    """Every electricity agreement on the account's current property."""
    props = account.get("properties") or []
    # The current home is the one without a moved-out date, else the latest.
    props = sorted(props, key=lambda p: (p.get("moved_out_at") is None, p.get("moved_in_at") or ""))
    out: list[Agreement] = []
    for prop in props[-1:]:
        for point in prop.get("electricity_meter_points") or []:
            for a in point.get("agreements") or []:
                parts = tariff_parts(a.get("tariff_code", ""))
                if parts:
                    out.append(Agreement(a["tariff_code"], parts["product"], _parse_time(a.get("valid_from")),
                                         _parse_time(a.get("valid_to")), bool(point.get("is_export"))))
    return out


def current(agreements_: list[Agreement], export: bool, now: datetime | None = None) -> Agreement | None:
    now = now or datetime.now(timezone.utc)
    live = [a for a in agreements_ if a.export == export and a.overlaps(now, now + timedelta(seconds=1))]
    return live[-1] if live else None


def half_hours(rows: list[dict], start: datetime, end: datetime) -> dict[datetime, float]:
    """Spread rate rows (any length, open-ended allowed) over UTC half hours."""
    out: dict[datetime, float] = {}
    for r in sorted(rows, key=lambda r: r.get("valid_from") or ""):
        a = max(_parse_time(r.get("valid_from")) or start, start)
        b = min(_parse_time(r.get("valid_to")) or end, end)
        a = a.replace(minute=a.minute // 30 * 30, second=0, microsecond=0)
        while a < b:
            out[a] = float(r["value_inc_vat"])
            a += timedelta(minutes=30)
    return out


def local_slot(ts: datetime, tz) -> tuple[str, int]:
    local = ts.astimezone(tz)
    return local.date().isoformat(), (local.hour * 60 + local.minute) // 30


# ---------- recommendations ----------


def blocks(points: list[tuple[datetime, float]], keep: Callable[[float], bool]) -> list[dict]:
    """Contiguous runs of half hours whose price passes `keep`."""
    out: list[dict] = []
    for ts, price in points:
        if not keep(price):
            continue
        if out and out[-1]["end"] == ts:
            run = out[-1]
            run["end"] = ts + timedelta(minutes=30)
            run["prices"].append(price)
        else:
            out.append({"start": ts, "end": ts + timedelta(minutes=30), "prices": [price]})
    return out


def _window(run: dict, kind: str, note: str = "") -> dict:
    prices = run["prices"]
    return {"start": _iso(run["start"]), "end": _iso(run["end"]), "kind": kind,
            "avg_p": round(sum(prices) / len(prices), 2), "min_p": round(min(prices), 2), "note": note}


def recommend(points: list[tuple[datetime, float]], charge_hours: float, efficiency: float = 0.9,
              export_points: list[tuple[datetime, float]] | None = None) -> list[dict]:
    """Suggested charge windows from upcoming import prices, cheapest first in time order.

    Fixed time-of-use tariffs (a few price levels) get their cheapest blocks.
    Agile-style tariffs get the cheapest stretch long enough to fill the
    battery, plus any half hour where Octopus pays you to use power. A window
    is only suggested for grid charging when it's cheaper than the dearer
    hours after allowing for round-trip losses.
    """
    if not points:
        return []
    prices = [p for _, p in points]
    levels = sorted({round(p, 2) for p in prices})
    out: list[dict] = []
    worth = lambda price: price < max(prices) * efficiency  # noqa: E731
    if len(levels) <= 4:
        cheapest = levels[0]
        for run in blocks(points, lambda p: round(p, 2) <= cheapest):
            if worth(min(run["prices"])):
                out.append(_window(run, "charge", "Cheapest rate"))
        if len(levels) >= 3:  # Flux and Cosy have a clear peak to avoid
            for run in blocks(points, lambda p: round(p, 2) >= levels[-1]):
                out.append(_window(run, "avoid", "Peak rate: run the house from the battery"))
    else:
        n = max(1, math.ceil(charge_hours * 2))
        best = None
        for i in range(0, max(1, len(points) - n + 1)):
            run = points[i:i + n]
            contiguous = all(run[j + 1][0] - run[j][0] == timedelta(minutes=30) for j in range(len(run) - 1))
            if contiguous:
                avg = sum(p for _, p in run) / len(run)
                if best is None or avg < best[0]:
                    best = (avg, i)
        if best and worth(best[0]):
            i = best[1]
            seg = points[i:i + n]
            out.append(_window({"start": seg[0][0], "end": seg[-1][0] + timedelta(minutes=30),
                                "prices": [p for _, p in seg]}, "charge", f"Cheapest {n / 2:g} hours"))
        for run in blocks(points, lambda p: p <= 0):
            out.append(_window(run, "charge", "Negative price: you're paid to use power"))
    if export_points:
        eprices = [p for _, p in export_points]
        top = max(eprices)
        if len({round(p, 2) for p in eprices}) > 1 and top > min(prices):
            for run in blocks(export_points, lambda p: p >= top - 0.01):
                out.append(_window(run, "export", "Best export price"))
    return sorted(out, key=lambda w: w["start"])


# ---------- service ----------


# Local half hours of an Economy 7 night (00:30-07:30), when the off-peak
# hours aren't known.
ECONOMY7_NIGHT = frozenset(range(1, 15))


class Octopus:
    """Keeps prices and dispatch slots in step with the account."""

    def __init__(self, storage, api_key: str, account: str, fetch: Fetch | None = None,
                 offpeak: Callable[[], set[int]] = lambda: set(ECONOMY7_NIGHT)) -> None:
        self.storage = storage
        self.offpeak = offpeak  # local half hours of the night rate
        self.client = Client(api_key, account, fetch) if api_key and account else None
        # Shown on the card when the settings themselves are wrong (e.g. OCTOPUS_ACCOUNT missing).
        self.config_error: str | None = None
        self.info: dict = {}
        self.diagnostics: dict = {}
        self.dispatch_slots: dict = {"planned": [], "completed": []}
        self.last_sync: float | None = None
        self.last_error: str | None = None
        self._agreements: list[Agreement] = []

    @property
    def configured(self) -> bool:
        return self.client is not None

    def sync(self, now: datetime | None = None) -> None:
        """Fetch the account, any missing prices and dispatch slots. Blocking."""
        if not self.client:
            return
        now = now or datetime.now(timezone.utc)
        acct = self.client.account
        try:
            log.info("Octopus: reading account %s", acct)
            self._agreements = agreements(self.client.account_info())
            log.info("Octopus: %s electricity agreement(s): %s", len(self._agreements),
                     ", ".join(f"{a.tariff} ({'export' if a.export else 'import'}, from {a.valid_from:%Y-%m-%d} "
                               f"to {a.valid_to:%Y-%m-%d})" if a.valid_from and a.valid_to else
                               f"{a.tariff} ({'export' if a.export else 'import'})" for a in self._agreements) or "none")
            imp, exp = current(self._agreements, False, now), current(self._agreements, True, now)
            if not imp:
                raise OctopusError("Octopus didn't list an active electricity tariff for this account. "
                                   "Check the account number, and that the account has electricity with Octopus.")
            self.info = {
                "import": self._describe(imp),
                "export": self._describe(exp) if exp else None,
            }
            log.info("Octopus: import tariff %s (%s), export tariff %s", imp.tariff, self.info["import"]["name"],
                     exp.tariff if exp else "none")
            stored = self._fill_prices(now)
            dispatch_error = None
            if tariff_kind(imp.product) == "intelligent_go":
                try:
                    self.dispatch_slots = self.client.dispatches()
                    self._apply_dispatches()
                    log.info("Octopus: %s planned and %s completed smart-charge slot(s)",
                             len(self.dispatch_slots["planned"]), len(self.dispatch_slots["completed"]))
                except OctopusError as err:  # prices still work without them
                    dispatch_error = f"Couldn't read Intelligent Go slots: {err}"
                    log.warning("Octopus: %s", dispatch_error)
            else:
                self.dispatch_slots = {"planned": [], "completed": []}
            current_price = self.upcoming(now)[0]
            self.diagnostics = {"host": self.client._host, "prices_stored": stored, "price_days": self.storage.rate_days(),
                                "agreements": [{"tariff": a.tariff, "export": a.export,
                                                "from": a.valid_from.isoformat() if a.valid_from else None,
                                                "to": a.valid_to.isoformat() if a.valid_to else None} for a in self._agreements]}
            if not current_price:
                raise OctopusError(f"Octopus returned no price for now for {imp.tariff} ({stored} half hours stored). "
                                   "Please share the 'Octopus:' lines from the container log.")
            self.last_sync = now.timestamp()
            self.last_error = dispatch_error
            log.info("Octopus: price now %.2fp, %s upcoming half hours known", current_price[0][1], len(current_price))
        except OctopusError as err:
            self.last_error = str(err)
            log.warning("Octopus: %s", err)
        except Exception as err:  # never let a bad answer stop the dashboard
            self.last_error = f"Unexpected answer from Octopus: {err}"
            log.exception("Octopus sync failed")

    def _describe(self, a: Agreement) -> dict:
        name = a.product
        try:
            p = self.client.product(a.product)
            name = p.get("display_name") or p.get("full_name") or name
        except OctopusError:
            pass
        sc = None
        if not a.export:
            try:
                sc = self.client.standing_charge(a.product, a.tariff)
            except OctopusError:
                pass
        return {"name": name, "product": a.product, "tariff": a.tariff, "kind": tariff_kind(a.product),
                "standing_charge_p": round(sc, 2) if sc is not None else None}

    def _fill_prices(self, now: datetime) -> int:
        """Prices from the start of recorded history (or the last day already
        stored) to as far ahead as Octopus has published."""
        tz = self.storage.tz
        first = self.storage.first_slot_day() or now.astimezone(tz).date().isoformat()
        start_day = date.fromisoformat(first)
        have = self.storage.rate_days()
        if have and have[0] <= first:
            start_day = max(start_day, date.fromisoformat(have[1]) - timedelta(days=1))
        start = datetime.combine(start_day, datetime.min.time(), tz).astimezone(timezone.utc)
        end = now + timedelta(days=2)
        rows: dict[tuple[str, int], list] = {}
        for a in self._agreements:
            if not a.overlaps(start, end):
                continue
            a_start = max(start, a.valid_from or start)
            a_end = min(end, a.valid_to or end)
            if a.two_rate and not a.export:
                prices = self._two_rate(a, a_start, a_end)
            else:
                prices = half_hours(self.client.unit_rates(a.product, a.tariff, a_start, a_end), a_start, a_end)
            log.info("Octopus: %s prices for %s from %s to %s", len(prices), a.tariff, _iso(a_start), _iso(a_end))
            col = 1 if a.export else 0
            for ts, p in prices.items():
                rows.setdefault(local_slot(ts, tz), [None, None])[col] = p
        self.storage.save_rates([(d, s, v[0], v[1]) for (d, s), v in rows.items()])
        return len(rows)

    def _two_rate(self, a: Agreement, start: datetime, end: datetime) -> dict[datetime, float]:
        """Economy 7 style: Octopus gives a day and a night price but not the
        hours, which depend on the meter. Use the off-peak window from the
        payback settings."""
        day = half_hours(self.client.unit_rates(a.product, a.tariff, start, end, "day"), start, end)
        night = half_hours(self.client.unit_rates(a.product, a.tariff, start, end, "night"), start, end)
        window = self.offpeak()
        tz = self.storage.tz
        out = {}
        for ts in set(day) | set(night):
            slot = local_slot(ts, tz)[1]
            out[ts] = night.get(ts, day.get(ts)) if slot in window else day.get(ts, night.get(ts))
        return out

    def refresh_dispatches(self) -> None:
        """Re-read only the Intelligent Go slots, between full syncs. Blocking.

        Slots can be added or cancelled minutes before they start, so this
        runs often; on failure the last list is kept until it expires."""
        if not self.client or (self.info.get("import") or {}).get("kind") != "intelligent_go":
            return
        try:
            slots = self.client.dispatches()
        except Exception as err:  # never let a bad answer stop the dashboard
            log.warning("Octopus: couldn't refresh Intelligent Go slots: %s", err)
            return
        def key(d):
            return d.get("start"), d.get("end")
        if sorted(map(key, slots["planned"])) != sorted(map(key, self.dispatch_slots.get("planned") or [])):
            log.info("Octopus: smart-charge slots now %s",
                     ", ".join(f"{d.get('start')}-{d.get('end')}" for d in slots["planned"]) or "none")
        self.dispatch_slots = slots
        self._apply_dispatches()

    def _apply_dispatches(self) -> None:
        """On Intelligent Octopus Go the whole home pays the off-peak price during
        smart-charge slots outside the usual night window. Octopus's price list
        doesn't show that, so mark past slots that were dispatched."""
        tz = self.storage.tz
        prices = self.storage.rates()
        rows = []
        for d in self.dispatch_slots.get("completed") or []:
            a, b = _parse_time(d.get("start")), _parse_time(d.get("end"))
            if not a or not b:
                continue
            day_prices = [p[0] for (day, _), p in prices.items() if day == local_slot(a, tz)[0] and p[0] is not None]
            if not day_prices:
                continue
            cheap = min(day_prices)
            ts = a.replace(minute=a.minute // 30 * 30, second=0, microsecond=0)
            while ts < b:
                rows.append((*local_slot(ts, tz), cheap, None))
                ts += timedelta(minutes=30)
        if rows:
            self.storage.save_rates(rows)

    # ---- for the API ----

    def upcoming(self, now: datetime | None = None) -> tuple[list, list]:
        """Known import and export prices from the current half hour on."""
        now = now or datetime.now(timezone.utc)
        tz = self.storage.tz
        start = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
        rates = self.storage.rates(since=start.astimezone(tz).date().isoformat())
        imp, exp = [], []
        ts = start
        while True:
            key = local_slot(ts, tz)
            if key not in rates:
                break
            i, e = rates[key]
            if i is not None:
                imp.append((ts, i))
            if e is not None:
                exp.append((ts, e))
            ts += timedelta(minutes=30)
        return imp, exp

    def status(self, battery: dict | None = None, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        out = {"configured": self.configured, "last_sync": self.last_sync,
               "last_error": self.config_error or self.last_error,
               "account": self.client.account if self.client else None, "diagnostics": self.diagnostics, **self.info}
        if not self.configured or not self.info:
            return out
        imp, exp = self.upcoming(now)
        battery = battery or {}
        kwh, kw = battery_size(battery)
        out["prices"] = [{"start": _iso(t), "import_p": p, "export_p": dict(exp).get(t)} for t, p in imp]
        out["current_p"] = imp[0][1] if imp else None
        out["current_export_p"] = exp[0][1] if exp else None
        out["recommendations"] = recommend(imp, kwh / kw if kw > 0 else 3, export_points=exp)
        planned = []
        for d in self.dispatch_slots.get("planned") or []:
            a, b = _parse_time(d.get("start")), _parse_time(d.get("end"))
            if a and b and b > now:
                planned.append({"start": _iso(a), "end": _iso(b)})
        out["dispatches"] = sorted(planned, key=lambda d: d["start"])
        return out

    def profile(self) -> dict | None:
        """A typical day for the payback form: the latest full day's prices
        reduced to peak, off-peak and its window, plus the export price."""
        rates = self.storage.rates()
        days: dict[str, dict[int, tuple]] = {}
        for (d, s), v in rates.items():
            days.setdefault(d, {})[s] = v
        full = [d for d, slots in days.items() if len([v for v in slots.values() if v[0] is not None]) >= 46]
        if not full:
            return None
        day = days[max(full)]
        imp = [day[s][0] if s in day and day[s][0] is not None else None for s in range(48)]
        known = [p for p in imp if p is not None]
        low, high = min(known), max(known)
        # Longest run of the cheapest price, wrapping past midnight.
        best = (0, 0)
        for s in range(48):
            if imp[s] == low and imp[(s - 1) % 48] != low:
                n = 0
                while n < 48 and imp[(s + n) % 48] == low:
                    n += 1
                best = max(best, (n, s), key=lambda x: x[0])
        n, s = best
        exports = [v[1] for v in day.values() if v[1] is not None]
        return {
            "peak_rate": round(high, 2),
            "offpeak_rate": round(low, 2),
            "offpeak_start": slot_time(s) if n and n < 48 else "00:00",
            "offpeak_end": slot_time(s + n) if n and n < 48 else "00:00",
            "export_rate": round(sum(exports) / len(exports), 2) if exports else None,
            "day": max(full),
        }


def validate(api_key: str, account: str) -> tuple[str, str]:
    api_key, account = api_key.strip(), account.strip().upper()
    if not KEY_RE.match(api_key):
        raise ValueError("The API key starts with sk_live_. Find it on octopus.energy under Personal details > API access.")
    if not ACCOUNT_RE.match(account):
        raise ValueError("The account number looks like A-1234ABCD.")
    return api_key, account
