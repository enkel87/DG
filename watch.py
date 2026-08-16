#!/usr/bin/env python3
"""
gotland_watch — monitor Destination Gotland availability.

Starts at destinationgotland.se (the user-friendly booking widget),
fills in the form per config, clicks SÖK RESOR, and parses the result
page for matching departures.

Usage:
    python watch.py                              # one-shot, reads config.yaml
    python watch.py --config my.yaml             # alternate config
    python watch.py --daemon                     # loop forever
    python watch.py --debug                      # save HTML + screenshots at every step
    python watch.py --no-headless                # show the browser window
    python watch.py --watch "Nyn-Visby 26 maj"   # run a single named watch
    python watch.py --inspect                    # just dump home-page HTML and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORTS = {"nynashamn": "Nynäshamn", "oskarshamn": "Oskarshamn", "visby": "Visby"}

VEHICLES = {
    # key -> (group_accordion_id, sub_option_label, style)
    # style: 'radio' for single-select radio options, 'amount' for +/- counter
    "none": None,
    # --- Personbil (kategoriserat efter höjd) ---
    "personbil":           ("car-vehicle-group_summary", "Personbil, under 2,25 m hög", "radio"),
    "personbil_hog":       ("car-vehicle-group_summary", "Personbil, över 2,25 m hög", "radio"),
    "personbil_husvagn":   ("car-vehicle-group_summary", "P-bil+Husvagn", "radio"),
    "personbil_slap":      ("car-vehicle-group_summary", "P-bil+Släpvagn", "radio"),
    "personbil_hastvagn":  ("car-vehicle-group_summary", "P-bil+Hästtransport", "radio"),
    # --- Husbil (kategoriserat efter längd) ---
    "husbil_under_6m":     ("mobile-home-vehicle-group_summary", "Husbil under 6 meter", "radio"),
    "husbil_6_till_9m":    ("mobile-home-vehicle-group_summary", "Husbil 6-9 meter", "radio"),
    "husbil_over_9m":      ("mobile-home-vehicle-group_summary", "Husbil över 9 meter", "radio"),
    # --- Lätt lastbil ---
    "lastbil_under_6m":    ("light-truck-vehicle-group_summary", "Lätt lastbil under 6 meter", "radio"),
    "lastbil_under_6m_slap": ("light-truck-vehicle-group_summary", "Lätt lastbil under 6m+Släp", "radio"),
    "lastbil_over_6m":     ("light-truck-vehicle-group_summary", "Lätt lastbil över 6 meter", "radio"),
    # --- Cykel/Moped/Mc (antalsväljare) ---
    "cykel":               ("bike-moped-mc-vehicle-group_summary", "Cykel/Moped", "amount"),
    "mc":                  ("bike-moped-mc-vehicle-group_summary", "Mc", "amount"),
    "mc_sidovagn":         ("bike-moped-mc-vehicle-group_summary", "Mc+sidovagn", "amount"),
}

# Passenger code (from id="ticket-item-Passenger-X-label") -> human label
PASSENGER_CODES = {
    "A": "Vuxen",
    "U": "Ungdom 19-25",
    "T": "Ungdom 13-18",
    "C": "Barn 3-12",
    "I": "Barn 0-2",
    "P": "Pensionär",
    "S": "Studerande",
}

BOOKING_URL = "https://www.destinationgotland.se/"
RESULTS_URL_FRAGMENT = "sok-resultat"


@dataclass
class Passengers:
    adults: int = 1       # 26+   (DG: Vuxen, code A)
    youth_19_25: int = 0  # 19-25 (DG: Ungdom 19-25, code U)
    youth: int = 0        # 13-18 (DG: Ungdom 13-18, code T)
    children: int = 0     # 3-12  (DG: Barn 3-12, code C)
    infants: int = 0      # 0-2   (DG: Barn 0-2, code I)
    seniors: int = 0      # 65+   (DG: Pensionär, code P)
    students: int = 0     #       (DG: Studerande, code S)

    def by_code(self, code: str) -> int:
        return {
            "A": self.adults, "U": self.youth_19_25, "T": self.youth,
            "C": self.children, "I": self.infants, "P": self.seniors,
            "S": self.students,
        }.get(code, 0)

    def total(self) -> int:
        return (self.adults + self.youth_19_25 + self.youth
                + self.children + self.infants + self.seniors + self.students)


@dataclass
class WatchConfig:
    name: str
    trip_type: str
    origin: str
    destination: str
    outbound_date: str
    return_date: str | None = None
    passengers: Passengers = field(default_factory=Passengers)
    vehicle: str = "none"
    departure_time: str | None = None    # exact "HH:MM" or null for any
    return_time: str | None = None
    departure_window: str | None = None  # "HH:MM-HH:MM"
    return_window: str | None = None

    def validate(self) -> None:
        assert self.trip_type in ("one_way", "return"), f"trip_type={self.trip_type}"
        assert self.origin in PORTS, f"origin={self.origin}"
        assert self.destination in PORTS, f"destination={self.destination}"
        assert self.origin != self.destination
        datetime.strptime(self.outbound_date, "%Y-%m-%d")
        if self.trip_type == "return":
            assert self.return_date, "return_date required for return trip"
            datetime.strptime(self.return_date, "%Y-%m-%d")
        assert self.vehicle in VEHICLES, f"vehicle={self.vehicle}"
        for fld, val in (("departure_window", self.departure_window),
                         ("return_window", self.return_window)):
            if val:
                parts = val.split("-")
                assert len(parts) == 2, f"{fld} must be HH:MM-HH:MM, got {val}"
                for p in parts:
                    datetime.strptime(p.strip(), "%H:%M")


@dataclass
class NotifierConfig:
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password_env: str = "SMTP_PASSWORD"
    email_from: str = ""
    email_to: str = ""
    # Env-var names used when the address fields above are left empty.
    # This lets the config file stay free of personal data in a public repo.
    smtp_user_env: str = "SMTP_USER"
    email_from_env: str = "EMAIL_FROM"
    email_to_env: str = "EMAIL_TO"
    pushover_enabled: bool = False
    pushover_token_env: str = "PUSHOVER_TOKEN"
    pushover_user_env: str = "PUSHOVER_USER"
    telegram_enabled: bool = False
    telegram_token_env: str = "TELEGRAM_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"

    def resolved_smtp_user(self) -> str:
        return self.smtp_user or os.environ.get(self.smtp_user_env, "")

    def resolved_email_from(self) -> str:
        return (self.email_from
                or os.environ.get(self.email_from_env, "")
                or self.resolved_smtp_user())

    def resolved_email_to(self) -> str:
        return (self.email_to
                or os.environ.get(self.email_to_env, "")
                or self.resolved_smtp_user())


@dataclass
class Settings:
    interval_seconds: int = 900
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    state_file: str = "state.json"
    notify_only_on_change: bool = True
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    watches: list[WatchConfig] = field(default_factory=list)


def _to_date_str(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def load_config(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text())
    notifier = NotifierConfig(**(raw.get("notifier") or {}))
    watches = []
    for w in raw.get("watches", []):
        pax = Passengers(**(w.pop("passengers", None) or {}))
        w["outbound_date"] = _to_date_str(w.get("outbound_date"))
        w["return_date"] = _to_date_str(w.get("return_date"))
        watches.append(WatchConfig(passengers=pax, **w))
    s = Settings(
        interval_seconds=int(raw.get("interval_seconds", 900)),
        headless=bool(raw.get("headless", True)),
        user_agent=raw.get("user_agent", Settings.user_agent),
        state_file=raw.get("state_file", "state.json"),
        notify_only_on_change=bool(raw.get("notify_only_on_change", True)),
        notifier=notifier,
        watches=watches,
    )
    for w in s.watches:
        w.validate()
    return s


# ---------------------------------------------------------------------------
# Debug dumper
# ---------------------------------------------------------------------------

class DebugDumper:
    def __init__(self, base: Path | None, watch_name: str):
        self.base = base
        self.watch_name = watch_name
        self.counter = 0
        if base:
            base.mkdir(parents=True, exist_ok=True)

    def dump(self, page: Page, label: str) -> None:
        if not self.base:
            return
        self.counter += 1
        ts = datetime.now().strftime("%H%M%S")
        safe_label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", self.watch_name)
        prefix = f"{ts}-{self.counter:02d}-{safe_name}-{safe_label}"
        try:
            (self.base / f"{prefix}.html").write_text(page.content())
            page.screenshot(path=str(self.base / f"{prefix}.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Page interactions
# ---------------------------------------------------------------------------

def _dismiss_cookies(page: Page, log: logging.Logger) -> None:
    candidates = [
        'button:has-text("Godkänn alla")',
        'button:has-text("Acceptera alla")',
        'button:has-text("Tillåt alla")',
        '#coi-banner-accept',
        '#onetrust-accept-btn-handler',
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible(timeout=1500):
                btn.click(timeout=2000)
                log.info("Clicked cookie button: %s", sel)
                break
        except Exception:  # noqa: BLE001
            continue
    try:
        page.wait_for_function(
            """() => {
                const b = document.querySelector('#coi-banner-wrapper, [id*="coi-banner"]');
                if (!b) return true;
                return b.getAttribute('aria-hidden') === 'true' || b.offsetParent === null;
            }""",
            timeout=5000,
        )
        log.info("Cookie banner dismissed")
    except PlaywrightTimeout:
        log.warning("Cookie banner may still be visible — continuing")


def _click_visible(page: Page, selectors: list[str], description: str,
                   log: logging.Logger, timeout: int = 4000,
                   optional: bool = False) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if loc.is_visible(timeout=1000):
                loc.click(timeout=timeout)
                log.info("%s: clicked via %s", description, sel)
                return True
        except Exception:  # noqa: BLE001
            continue
    if not optional:
        log.warning("%s: no visible match", description)
    return False


def _set_trip_type(page: Page, watch: WatchConfig, log: logging.Logger) -> None:
    want_return = watch.trip_type == "return"
    try:
        switch = page.get_by_role("switch").first
        if switch.count() == 0:
            switch = page.locator(
                'input[type="checkbox"][role="switch"], '
                'label:has-text("Tur och retur") input'
            ).first
        if switch.count() == 0:
            log.warning("Trip type switch not found")
            return
        is_checked = False
        try:
            is_checked = switch.is_checked()
        except Exception:  # noqa: BLE001
            try:
                is_checked = (switch.get_attribute("aria-checked") == "true")
            except Exception:  # noqa: BLE001
                pass
        if is_checked != want_return:
            label = page.locator('label:has-text("Tur och retur")').first
            target = label if label.count() else switch
            target.click(timeout=3000)
            log.info("Toggled trip type to %s", "return" if want_return else "one_way")
        else:
            log.info("Trip type already correct (%s)",
                     "return" if want_return else "one_way")
    except Exception as e:  # noqa: BLE001
        log.warning("Trip type toggle failed: %s", e)


def _set_route(page: Page, watch: WatchConfig, log: logging.Logger,
               dump: DebugDumper) -> None:
    origin_label = PORTS[watch.origin]
    dest_label = PORTS[watch.destination]
    # DG uses arrow inside the modal, hyphen on the home-page button.
    route_arrow = f"{origin_label} → {dest_label}"
    route_hyphen = f"{origin_label} - {dest_label}"

    # Always open the picker — we cannot tell from outside which direction
    # is currently selected, and the default may be the opposite of what
    # we want.
    opened = _click_visible(
        page,
        [
            'button:has-text("Enkel resa")',
            'button:has-text("Tur och retur"):not(:has(input))',
            'button:has-text("Välj resväg")',
            'button:has-text("Nynäshamn - Visby")',
            'button:has-text("Visby - Nynäshamn")',
            'button:has-text("Nynäshamn - Oskarshamn")',
            'button:has-text("Oskarshamn - Visby")',
            'button:has-text("Visby - Oskarshamn")',
        ],
        "Open route picker",
        log,
    )
    page.wait_for_timeout(700)
    dump.dump(page, "route-opened")
    if not opened:
        log.warning("Could not open route picker; continuing")
        return

    # Pick the route inside the modal (arrow format is what DG shows).
    # The arrow between ports is likely an SVG icon, so text selectors
    # matching "Origin → Dest" won't work. Use JS to find any clickable
    # element containing both port names with origin appearing first,
    # and NOT containing any third port name.
    picked = False
    try:
        js_result = page.evaluate(
            """({origin, dest}) => {
                const candidates = document.querySelectorAll(
                    'label, [role="option"], [role="radio"], button, li, ' +
                    'div[role="button"]'
                );
                const ports = ['Nynäshamn', 'Visby', 'Oskarshamn'];
                for (const el of candidates) {
                    const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const i1 = t.indexOf(origin);
                    const i2 = t.indexOf(dest);
                    if (i1 < 0 || i2 <= i1) continue;
                    // Skip elements that mention a third unrelated port
                    const others = ports.filter(p => p !== origin && p !== dest);
                    if (others.some(p => t.includes(p))) continue;
                    // Skip long text (probably a container with more than one row)
                    if (t.length > 60) continue;
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return {ok: true, text: t};
                }
                return {ok: false};
            }""",
            {"origin": origin_label, "dest": dest_label},
        )
        if isinstance(js_result, dict) and js_result.get("ok"):
            log.info("Picked route %s → %s (text=%r)",
                     origin_label, dest_label, js_result.get("text"))
            picked = True
        else:
            log.warning("Route JS: no matching option found")
    except Exception as e:  # noqa: BLE001
        log.warning("Route JS click failed: %s", e)

    # Close the modal
    closed = False
    if picked:
        closed = _click_visible(
            page,
            ['button:has-text("Klar")', 'button:has-text("OK")',
             'button:has-text("Spara")', 'button:has-text("Välj")',
             'button:has-text("Bekräfta")'],
            "Confirm route",
            log,
            timeout=2000,
        )
    if not closed:
        page.wait_for_timeout(500)
        for close_sel in ['button[aria-label*="Stäng" i]',
                          'button[aria-label*="close" i]']:
            try:
                cb = page.locator(close_sel).first
                if cb.count() and cb.is_visible(timeout=500):
                    cb.click(timeout=1500)
                    closed = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not closed:
            try:
                page.keyboard.press("Escape")
                log.info("Pressed Escape to close route modal")
            except Exception:  # noqa: BLE001
                pass
    page.wait_for_timeout(700)


_MONTHS_SV = [
    "januari", "februari", "mars", "april", "maj", "juni",
    "juli", "augusti", "september", "oktober", "november", "december",
]


def _set_date(page: Page, watch: WatchConfig, log: logging.Logger,
              dump: DebugDumper) -> None:
    out_dt = datetime.strptime(watch.outbound_date, "%Y-%m-%d")

    opened = _click_visible(
        page,
        [
            'button:has-text("Datum")',
            'div:has-text("Datum") + div button',
        ],
        "Open date picker",
        log,
    )
    page.wait_for_timeout(700)
    dump.dump(page, "date-opened")
    if not opened:
        return

    def _pick(dt: datetime, label: str) -> None:
        target_month = _MONTHS_SV[dt.month - 1]
        target_substr = f"{dt.day} {target_month} {dt.year}"

        # Wait for the calendar grid to render
        try:
            page.wait_for_selector('[role="gridcell"]', timeout=5000, state="visible")
        except PlaywrightTimeout:
            log.warning("Date picker grid did not render")

        # Navigate to right month if needed
        for _ in range(24):
            try:
                header = page.locator(
                    f'text=/{target_month}\\s+{dt.year}/i'
                ).first
                if header.count() and header.is_visible(timeout=500):
                    break
            except Exception:  # noqa: BLE001
                pass
            clicked_nav = False
            for sel in [
                'button[aria-label*="Nästa månad" i]',
                'button[aria-label*="next month" i]',
                'button[aria-label="Nästa datum"]',
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.count() and btn.is_visible(timeout=500):
                        btn.click(timeout=1000)
                        clicked_nav = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked_nav:
                break
            page.wait_for_timeout(150)

        # Match the date cell using JS evaluation. Two strategies:
        # 1) aria-label substring match (e.g. "tisdag 26 maj 2026")
        # 2) leading day-number match. DG cells show day + price like
        #    "26503:-" so we match '^26' followed by 0 or a price suffix
        #    (3-4 digits + ":-"). Safe against the "22624 contains 26" trap
        #    because we check LEADING digits only.
        page.wait_for_timeout(600)  # let aria-labels settle if added async
        clicked = False
        try:
            js_result = page.evaluate(
                """({target_day, target_substr}) => {
                    const cells = document.querySelectorAll('[role="gridcell"]');
                    // Day-num regex: starts with day, then optional price suffix
                    const dayStr = String(target_day);
                    const dayRe = new RegExp('^' + dayStr + '(\\\\d{3,4}:-)?$');
                    const samples = [];
                    for (let i = 0; i < Math.min(cells.length, 8); i++) {
                        samples.push({
                            al: cells[i].getAttribute('aria-label') || '',
                            txt: (cells[i].textContent || '').replace(/\\s+/g, '').trim().slice(0, 30),
                            cls: (cells[i].className || '').toString().slice(0, 100),
                        });
                    }
                    for (const cell of cells) {
                        const cls = (cell.className || '').toString();
                        if (cls.includes('Mui-disabled')) continue;
                        if (cell.hasAttribute('disabled')) continue;

                        const al = cell.getAttribute('aria-label') || '';
                        const txt = (cell.textContent || '').replace(/\\s+/g, '').trim();

                        const matchAria = al && al.toLowerCase().includes(target_substr.toLowerCase());
                        const matchText = dayRe.test(txt);

                        if (matchAria || matchText) {
                            cell.scrollIntoView({block: 'center'});
                            cell.click();
                            return {
                                ok: true,
                                aria_label: al,
                                text: txt.slice(0, 30),
                                via: matchAria ? 'aria-label' : 'text-leading-digit',
                                total: cells.length,
                            };
                        }
                    }
                    return {ok: false, total: cells.length, samples: samples};
                }""",
                {"target_day": dt.day, "target_substr": target_substr},
            )
            if isinstance(js_result, dict) and js_result.get("ok"):
                log.info("Picked %s date %s via %s (text=%r)",
                         label, dt.date(), js_result.get("via"),
                         js_result.get("text"))
                clicked = True
            else:
                log.warning("[date] No matching cell. Total=%s",
                            (js_result or {}).get("total"))
                for s in (js_result or {}).get("samples", []):
                    log.warning("[date]   cell aria=%r text=%r cls=%r",
                                s.get("al"), s.get("txt"), s.get("cls"))
        except Exception as e:  # noqa: BLE001
            log.warning("JS date click failed: %s", e)

        if not clicked:
            log.warning("Could not click %s date %s — closing picker.",
                        label, dt.date())
            for close_sel in [
                'button[aria-label*="Stäng" i]',
                'button[aria-label*="close" i]',
            ]:
                try:
                    cb = page.locator(close_sel).first
                    if cb.count() and cb.is_visible(timeout=500):
                        cb.click(timeout=1500)
                        break
                except Exception:  # noqa: BLE001
                    continue
            else:
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(400)

    _pick(out_dt, "outbound")
    page.wait_for_timeout(300)
    if watch.return_date:
        _pick(datetime.strptime(watch.return_date, "%Y-%m-%d"), "return")
        page.wait_for_timeout(300)
    _click_visible(
        page,
        ['button:has-text("Klar")', 'button:has-text("OK")', 'button:has-text("Spara")'],
        "Confirm date",
        log,
        timeout=2000,
        optional=True,
    )
    page.wait_for_timeout(500)


def _adjust_passenger(page: Page, code: str, target: int, direction: str,
                      log: logging.Logger) -> int | None:
    """Adjust a single passenger row. direction = 'add' or 'remove'."""
    try:
        label_node = page.locator(f'#ticket-item-Passenger-{code}-label').first
        if label_node.count() == 0:
            return None
        row = label_node.locator(
            'xpath=ancestor::div[.//button[@aria-label="Öka antal"]][1]'
        )
        if row.count() == 0:
            return None
        spin = row.locator('[role="spinbutton"]').first
        current = 0
        if spin.count():
            try:
                current = int(spin.get_attribute("aria-valuenow") or "0")
            except Exception:  # noqa: BLE001
                current = 0
        plus = row.locator('button[aria-label="Öka antal"]').first
        minus = row.locator('button[aria-label="Minska antal"]').first
        tries = 0
        if direction == "add":
            while current < target and tries < 12:
                if plus.is_disabled():
                    log.info("Passenger %s: + disabled at current=%d", code, current)
                    break
                plus.click(timeout=1500)
                page.wait_for_timeout(120)
                if spin.count():
                    try:
                        current = int(spin.get_attribute("aria-valuenow") or str(current + 1))
                    except Exception:  # noqa: BLE001
                        current += 1
                else:
                    current += 1
                tries += 1
        else:  # remove
            while current > target and tries < 12:
                if minus.is_disabled():
                    log.info("Passenger %s: - disabled at current=%d (target=%d)",
                             code, current, target)
                    break
                minus.click(timeout=1500)
                page.wait_for_timeout(120)
                if spin.count():
                    try:
                        current = int(spin.get_attribute("aria-valuenow") or str(current - 1))
                    except Exception:  # noqa: BLE001
                        current -= 1
                else:
                    current -= 1
                tries += 1
        return current
    except Exception as e:  # noqa: BLE001
        log.warning("Passenger %s direction=%s failed: %s", code, direction, e)
        return None


def _set_passengers(page: Page, pax: Passengers, log: logging.Logger,
                    dump: DebugDumper) -> None:
    opened = _click_visible(
        page,
        ['button:has-text("Resenärer")'],
        "Open passenger picker",
        log,
    )
    page.wait_for_timeout(700)
    dump.dump(page, "passengers-opened")
    if not opened:
        return

    # Pass 1: ADD what should be there. DG requires at least one "primary"
    # passenger (vuxen/pensionär), so we must add before we can remove the
    # default vuxen.
    log.info("Passenger pass 1: additions")
    for code in PASSENGER_CODES:
        target = pax.by_code(code)
        if target > 0:
            final = _adjust_passenger(page, code, target, "add", log)
            if final is not None:
                log.info("Passenger %s (%s) -> %d (target %d)",
                         code, PASSENGER_CODES[code], final, target)

    # Pass 2: REMOVE categories that should be 0 (e.g. the default vuxen)
    log.info("Passenger pass 2: removals")
    for code in PASSENGER_CODES:
        target = pax.by_code(code)
        if target == 0:
            final = _adjust_passenger(page, code, 0, "remove", log)
            if final is not None and final != 0:
                log.info("Passenger %s could not reach 0 (stuck at %d)", code, final)

    _click_visible(
        page,
        ['button:has-text("Klar")', 'button:has-text("OK")', 'button:has-text("Spara")'],
        "Close passenger panel",
        log,
        timeout=2000,
    )
    page.wait_for_timeout(500)


def _set_vehicle(page: Page, watch: WatchConfig, log: logging.Logger,
                 dump: DebugDumper) -> None:
    if watch.vehicle == "none":
        log.info("Vehicle: none (skipping)")
        return
    spec = VEHICLES.get(watch.vehicle)
    if not spec:
        log.warning("Unknown vehicle key: %s", watch.vehicle)
        return
    group_id, label, style = spec

    opened = _click_visible(
        page,
        ['button:has-text("Fordon")'],
        "Open vehicle picker",
        log,
    )
    page.wait_for_timeout(700)
    dump.dump(page, "vehicle-opened")
    if not opened:
        return

    # Expand the accordion group (Personbil / Husbil / Lätt lastbil / Cykel-mc)
    try:
        accordion = page.locator(f'#{group_id}').first
        if accordion.count() == 0:
            log.warning("Vehicle group accordion #%s not found", group_id)
            return
        expanded = (accordion.get_attribute("aria-expanded") == "true")
        if not expanded:
            accordion.click(timeout=2500)
            page.wait_for_timeout(400)
            log.info("Expanded vehicle group: %s", group_id)
        else:
            log.info("Vehicle group %s already expanded", group_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to expand vehicle group %s: %s", group_id, e)
        return

    # Pick the sub-option
    if style == "radio":
        picked = False
        for sel in [
            f'.ToggleItem:has-text("{label}")',
            f'[role="radio"]:has-text("{label}")',
            f'label:has-text("{label}")',
            f'div:has-text("{label}") >> input[type="radio"]',
            f'text="{label}"',
        ]:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible(timeout=800):
                    el.click(timeout=2000)
                    log.info("Selected vehicle: %s", label)
                    picked = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not picked:
            log.warning("Vehicle radio option not found: %s", label)
    elif style == "amount":
        try:
            label_node = page.locator(f'text="{label}"').first
            if label_node.count() == 0:
                log.warning("Vehicle amount label not found: %s", label)
            else:
                row = label_node.locator(
                    'xpath=ancestor::div[.//button[@aria-label="Öka antal"]][1]'
                )
                plus = row.locator('button[aria-label="Öka antal"]').first
                if plus.count() and not plus.is_disabled():
                    plus.click(timeout=2000)
                    log.info("Vehicle %s: incremented by 1", label)
                else:
                    log.warning("Vehicle %s: + button missing or disabled", label)
        except Exception as e:  # noqa: BLE001
            log.warning("Vehicle amount-click %s failed: %s", label, e)

    _click_visible(
        page,
        ['button:has-text("Klar")', 'button:has-text("OK")', 'button:has-text("Spara")'],
        "Close vehicle picker",
        log,
        timeout=2000,
    )
    page.wait_for_timeout(500)


def _click_search(page: Page, log: logging.Logger) -> bool:
    page.wait_for_timeout(500)
    return _click_visible(
        page,
        [
            'button:has-text("SÖK RESOR")',
            'button:has-text("Sök resor")',
            'button:has-text("Sök resa")',
            'button:has-text("SÖK")',
            'button:has-text("Sök"):not([disabled])',
        ],
        "Click search",
        log,
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

@dataclass
class Departure:
    date: str
    time: str
    route: str
    arrival_time: str | None = None
    prices: dict[str, str] = field(default_factory=dict)
    available: bool | None = None
    raw_label: str | None = None


_TIME_RE = re.compile(r"\b([0-2]?\d:[0-5]\d)\b")
# Match a departure block: time time Mini X Flexi Y [Flexi+ Z]
# where X/Y/Z is either "Slutsålt" or a price like "878:-" or "1 141:-"
_DEPARTURE_RE = re.compile(
    r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+"
    r"Mini\s+(Slutsålt|[\d\s]+(?::-|kr))\s+"
    r"Flexi\s+(Slutsålt|[\d\s]+(?::-|kr))"
    r"(?:\s+Flexi\s*\+?\s+(Slutsålt|[\d\s]+(?::-|kr)))?",
    re.IGNORECASE,
)
SOLD_OUT_TOKENS = ("slutsålt", "slutsåld", "fullbokat", "fullt", "ej tillgänglig")


def _extract_departures(page: Page, watch: WatchConfig, side: str,
                        log: logging.Logger) -> list[Departure]:
    """Parse departures from the result page body text using regex.
    Robust against CSS class changes; depends only on visible Swedish labels."""
    route_label = (f"{PORTS[watch.origin]} → {PORTS[watch.destination]}"
                   if side == "outbound"
                   else f"{PORTS[watch.destination]} → {PORTS[watch.origin]}")
    try:
        body_text = page.locator("body").inner_text()
    except Exception:  # noqa: BLE001
        log.warning("[%s] could not read body text", side)
        return []

    # Split outbound vs return using DG's section headings
    out_marker = "Välj avresa"
    ret_marker = "Välj returresa"
    if side == "outbound":
        if ret_marker in body_text:
            text = body_text.split(ret_marker)[0]
        else:
            text = body_text
    else:
        if ret_marker not in body_text:
            return []
        text = body_text.split(ret_marker)[1]

    # Narrow further to start at the outbound/return marker if present
    if out_marker in text and side == "outbound":
        text = text[text.find(out_marker):]

    out: list[Departure] = []
    seen: set[str] = set()
    for m in _DEPARTURE_RE.finditer(text):
        dep_time, arr_time, mini, flexi, flexi_plus = m.groups()
        key = f"{dep_time}->{arr_time}"
        if key in seen:
            continue
        seen.add(key)
        prices: dict[str, str] = {}
        slotwise = {"Mini": mini, "Flexi": flexi}
        if flexi_plus:
            slotwise["Flexi+"] = flexi_plus
        for k, v in slotwise.items():
            if v.lower() not in SOLD_OUT_TOKENS:
                prices[k] = v.strip()

        # Available if AT LEAST one ticket type is not sold out
        available = any(v.lower() not in SOLD_OUT_TOKENS for v in slotwise.values())

        out.append(Departure(
            date=watch.outbound_date if side == "outbound" else (watch.return_date or ""),
            time=dep_time,
            arrival_time=arr_time,
            route=route_label,
            prices=prices,
            available=available,
            raw_label=f"Mini={mini} Flexi={flexi}"
                      + (f" Flexi+={flexi_plus}" if flexi_plus else ""),
        ))
    log.info("[%s] parsed %d departures", side, len(out))
    return out


def scrape(watch: WatchConfig, settings: Settings, debug_dir: Path | None,
           log: logging.Logger) -> dict[str, list[Departure]]:
    dump = DebugDumper(debug_dir, watch.name)
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=settings.headless)
        context: BrowserContext = browser.new_context(
            user_agent=settings.user_agent,
            locale="sv-SE",
            timezone_id="Europe/Stockholm",
            viewport={"width": 1400, "height": 900},
        )
        page: Page = context.new_page()
        try:
            page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            dump.dump(page, "01-home-loaded")
            _dismiss_cookies(page, log)
            page.wait_for_timeout(500)
            dump.dump(page, "02-after-cookies")
            _set_trip_type(page, watch, log)
            dump.dump(page, "03-after-trip-type")
            _set_route(page, watch, log, dump)
            dump.dump(page, "04-after-route")
            _set_date(page, watch, log, dump)
            dump.dump(page, "05-after-date")
            _set_passengers(page, watch.passengers, log, dump)
            dump.dump(page, "06-after-passengers")
            _set_vehicle(page, watch, log, dump)
            dump.dump(page, "07-after-vehicle")
            _click_search(page, log)
            try:
                page.wait_for_url(f"**{RESULTS_URL_FRAGMENT}**", timeout=15000)
                log.info("Results page reached: %s", page.url)
            except PlaywrightTimeout:
                log.warning("URL did not change to results; at: %s", page.url)
            # Wait until departure data has actually rendered. We look for the
            # combined pattern of time + ticket type (Mini/Flexi/Slutsålt).
            try:
                page.wait_for_function(
                    r"""() => {
                        const t = document.body.innerText;
                        return /\d{1,2}:\d{2}\s+\d{1,2}:\d{2}\s+Mini/.test(t);
                    }""",
                    timeout=20000,
                )
                log.info("Departure rows rendered")
            except PlaywrightTimeout:
                log.warning("Departure rows did not render within 20s — page may be empty")
            page.wait_for_timeout(1500)
            dump.dump(page, "08-results")

            outbound = _extract_departures(page, watch, "outbound", log)
            return_deps: list[Departure] = []
            if watch.trip_type == "return":
                return_deps = _extract_departures(page, watch, "return", log)
            log.info("Parsed %d outbound, %d return for %s",
                     len(outbound), len(return_deps), watch.name)
            # If we got nothing, dump a body-text snippet so we can diagnose
            if not outbound and not return_deps:
                try:
                    body_text = page.locator("body").inner_text()
                    log.warning("Body text snippet (first 600 chars):\n%s",
                                body_text[:600].replace("\n", " | "))
                except Exception:  # noqa: BLE001
                    pass
            return {"outbound": outbound, "return": return_deps}
        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _send_email(cfg: NotifierConfig, subject: str, body: str,
                log: logging.Logger) -> None:
    pw = os.environ.get(cfg.smtp_password_env)
    if not pw:
        log.error("SMTP password env var %s not set", cfg.smtp_password_env)
        return
    smtp_user = cfg.resolved_smtp_user()
    email_to = cfg.resolved_email_to()
    if not smtp_user:
        log.error("No SMTP user: set smtp_user in config or env %s",
                  cfg.smtp_user_env)
        return
    if not email_to:
        log.error("No recipient: set email_to in config or env %s",
                  cfg.email_to_env)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg.resolved_email_from()
    msg["To"] = email_to
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as s:
        s.starttls()
        s.login(smtp_user, pw)
        s.send_message(msg)
    log.info("Email sent: %s", subject)


def _send_pushover(cfg: NotifierConfig, title: str, message: str,
                   log: logging.Logger) -> None:
    import urllib.request
    import urllib.parse
    token = os.environ.get(cfg.pushover_token_env)
    user = os.environ.get(cfg.pushover_user_env)
    if not token or not user:
        log.error("Pushover env vars missing")
        return
    data = urllib.parse.urlencode({
        "token": token, "user": user, "title": title, "message": message,
    }).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
    urllib.request.urlopen(req, timeout=10).read()
    log.info("Pushover sent: %s", title)


def _send_telegram(cfg: NotifierConfig, message: str, log: logging.Logger) -> None:
    import urllib.request
    import urllib.parse
    token = os.environ.get(cfg.telegram_token_env)
    chat_id = os.environ.get(cfg.telegram_chat_id_env)
    if not token or not chat_id:
        log.error("Telegram env vars missing")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    urllib.request.urlopen(url, data=data, timeout=10).read()
    log.info("Telegram sent")


def notify(cfg: NotifierConfig, title: str, body: str, log: logging.Logger) -> None:
    if cfg.email_enabled:
        try: _send_email(cfg, title, body, log)
        except Exception as e: log.exception("Email failed: %s", e)  # noqa: BLE001
    if cfg.pushover_enabled:
        try: _send_pushover(cfg, title, body, log)
        except Exception as e: log.exception("Pushover failed: %s", e)  # noqa: BLE001
    if cfg.telegram_enabled:
        try: _send_telegram(cfg, f"{title}\n\n{body}", log)
        except Exception as e: log.exception("Telegram failed: %s", e)  # noqa: BLE001


# ---------------------------------------------------------------------------
# Matching + state
# ---------------------------------------------------------------------------

def matches_criteria(d: Departure, target_time: str | None,
                     target_window: str | None) -> bool:
    if d.available is False:
        return False
    if target_time and d.time != target_time:
        return False
    if target_window:
        start_s, end_s = [p.strip() for p in target_window.split("-")]
        if not (start_s <= d.time <= end_s):
            return False
    return d.available is True


def departure_key(d: Departure) -> str:
    return f"{d.date}|{d.time}|{d.route}|avail={d.available}"


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(settings: Settings, debug_dir: Path | None,
             only_watch: str | None, log: logging.Logger) -> None:
    state_path = Path(settings.state_file)
    state = load_state(state_path)

    for watch in settings.watches:
        if only_watch and watch.name != only_watch:
            continue
        log.info("=== Watch: %s ===", watch.name)
        try:
            deps_by_side = scrape(watch, settings, debug_dir, log)
        except Exception as e:  # noqa: BLE001
            log.exception("Scrape failed for %s: %s", watch.name, e)
            continue

        if not (deps_by_side["outbound"] + deps_by_side["return"]):
            log.info("No departures parsed; nothing to notify")
            continue

        # Always log the full parsed list so user can see what's available
        log.info("Parsed departures for %s:", watch.name)
        for d in deps_by_side["outbound"]:
            status = "TILLGÄNGLIG" if d.available else "Slutsåld" if d.available is False else "?"
            prices = ", ".join(f"{k} {v}" for k, v in d.prices.items()) or "-"
            log.info("  [utresa] %s -> %s  %s  %s",
                     d.time, d.arrival_time or "?", status, prices)
        for d in deps_by_side["return"]:
            status = "TILLGÄNGLIG" if d.available else "Slutsåld" if d.available is False else "?"
            prices = ", ".join(f"{k} {v}" for k, v in d.prices.items()) or "-"
            log.info("  [retur ] %s -> %s  %s  %s",
                     d.time, d.arrival_time or "?", status, prices)

        hits = [d for d in deps_by_side["outbound"]
                if matches_criteria(d, watch.departure_time, watch.departure_window)]
        if watch.trip_type == "return":
            return_hits = [d for d in deps_by_side["return"]
                           if matches_criteria(d, watch.return_time, watch.return_window)]
            hits = (hits + return_hits) if (hits and return_hits) else []

        log.info("%d hit(s) for %s", len(hits), watch.name)

        # --- State-change notification -----------------------------------
        # We notify on TRANSITIONS in both directions, never on repetition:
        #   nothing -> available   => "PLATS SLÄPPT"   (book now!)
        #   available -> nothing   => "FULLBOKAT IGEN" (so you know to wait)
        #   available -> available => silence
        #   nothing   -> nothing   => silence
        # State stores the sorted list of departure keys that were available
        # at the previous run for this watch.
        hit_keys = sorted(departure_key(d) for d in hits)
        prev_keys = state.get(watch.name, [])
        was_available = bool(prev_keys)
        is_available = bool(hit_keys)

        def _fmt_lines(deps: list[Departure]) -> str:
            return "\n".join(
                f"{d.time}{(' → ' + d.arrival_time) if d.arrival_time else ''}  "
                f"{d.route}  "
                + (", ".join(f"{k} {v}" for k, v in d.prices.items())
                   if d.prices else "")
                for d in deps
            )

        def _header() -> str:
            return (
                f"Watch: {watch.name}\n"
                f"Datum: {watch.outbound_date}"
                + (f" / retur {watch.return_date}" if watch.return_date else "")
                + f"\nResenärer: {watch.passengers.total()}\n"
                f"Fordon: {watch.vehicle}\n"
            )

        if not settings.notify_only_on_change and is_available:
            # notify_only_on_change disabled: mail on every run with a hit
            body = _header() + "\nTillgängliga avgångar:\n" + _fmt_lines(hits)
            notify(settings.notifier,
                   f"[Gotland] Plats ledig: {watch.name}", body, log)
            log.info("HIT (every-run mode):\n%s", body)

        elif is_available and not was_available:
            # Transition: sold out (or first ever run) -> available
            body = (_header()
                    + "\nPLATS SLÄPPT — boka nu:\n" + _fmt_lines(hits)
                    + "\n\nhttps://www.destinationgotland.se/")
            notify(settings.notifier,
                   f"[Gotland] ✅ PLATS SLÄPPT: {watch.name}", body, log)
            log.info("TRANSITION available:\n%s", body)

        elif is_available and was_available:
            new_keys = [k for k in hit_keys if k not in prev_keys]
            if new_keys:
                # Still available, but a NEW departure matched as well
                new_deps = [d for d in hits if departure_key(d) in new_keys]
                body = (_header()
                        + "\nYTTERLIGARE AVGÅNG SLÄPPT:\n" + _fmt_lines(new_deps)
                        + "\n\nAlla lediga just nu:\n" + _fmt_lines(hits)
                        + "\n\nhttps://www.destinationgotland.se/")
                notify(settings.notifier,
                       f"[Gotland] ✅ Fler platser: {watch.name}", body, log)
                log.info("TRANSITION extra departure:\n%s", body)
            else:
                log.info("Still available, no change — no mail sent")

        elif was_available and not is_available:
            # Transition: available -> sold out
            body = (_header()
                    + "\nFULLBOKAT IGEN. Bevakningen fortsätter och mejlar "
                      "så snart platser släpps på nytt.")
            notify(settings.notifier,
                   f"[Gotland] ⛔ Fullbokat igen: {watch.name}", body, log)
            log.info("TRANSITION sold out for %s", watch.name)

        else:
            log.info("Still sold out, no change — no mail sent")

        # Always persist current status so the next run can detect a change.
        if hit_keys:
            state[watch.name] = hit_keys
        else:
            state.pop(watch.name, None)
    save_state(state_path, state)


def inspect_home(settings: Settings, log: logging.Logger) -> None:
    out_dir = Path("debug")
    out_dir.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.headless)
        context = browser.new_context(
            user_agent=settings.user_agent,
            locale="sv-SE",
            timezone_id="Europe/Stockholm",
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()
        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        _dismiss_cookies(page, log)
        page.wait_for_timeout(2000)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        (out_dir / f"home-{ts}.html").write_text(page.content())
        page.screenshot(path=str(out_dir / f"home-{ts}.png"), full_page=True)
        log.info("Saved debug/home-%s.html and .png", ts)
        context.close()
        browser.close()


def _human_passengers(pax: Passengers) -> str:
    parts = []
    for code, label in PASSENGER_CODES.items():
        n = pax.by_code(code)
        if n > 0:
            parts.append(f"{n} {label}")
    return ", ".join(parts) if parts else "(inga)"


def _human_vehicle(key: str) -> str:
    if key == "none":
        return "(inget)"
    spec = VEHICLES.get(key)
    if not spec:
        return f"(okänd: {key})"
    return spec[1]


def validate_config_command(path: Path) -> int:
    """Load the config, validate everything, and print a friendly summary
    in Swedish. Returns 0 if all OK, 1 if any errors found."""
    print(f"Validerar {path}...\n")
    try:
        settings = load_config(path)
    except FileNotFoundError:
        print(f"  ✗ Filen {path} finns inte.")
        return 1
    except yaml.YAMLError as e:
        print(f"  ✗ YAML-syntaxfel: {e}")
        return 1
    except TypeError as e:
        print(f"  ✗ Okänt fält eller felaktig typ: {e}")
        print("    Tips: kolla att alla fältnamn är rättstavade")
        return 1
    except AssertionError as e:
        print(f"  ✗ Ogiltigt värde: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ Fel vid inläsning: {type(e).__name__}: {e}")
        return 1

    errors = 0
    warnings = 0
    today = datetime.now().date()

    print(f"=== {len(settings.watches)} bevakning(ar) ===\n")
    for i, w in enumerate(settings.watches, 1):
        print(f"  {i}. {w.name}")
        out_dt = datetime.strptime(w.outbound_date, "%Y-%m-%d").date()
        out_days = (out_dt - today).days
        if out_days < 0:
            print(f"     ✗ Utresedatum {out_dt} är i det förflutna ({-out_days} dagar sedan)")
            errors += 1
        else:
            print(f"     Sträcka:    {PORTS[w.origin]} → {PORTS[w.destination]} "
                  f"({'tur och retur' if w.trip_type == 'return' else 'enkel resa'})")
            print(f"     Utresedatum: {out_dt}  ({'idag' if out_days == 0 else f'om {out_days} dagar'})")
        if w.trip_type == "return" and w.return_date:
            ret_dt = datetime.strptime(w.return_date, "%Y-%m-%d").date()
            ret_days = (ret_dt - today).days
            if ret_dt < out_dt:
                print(f"     ✗ Returdatum {ret_dt} är före utresedatumet")
                errors += 1
            else:
                print(f"     Returdatum:  {ret_dt}  (om {ret_days} dagar)")
        print(f"     Resenärer:   {_human_passengers(w.passengers)} (totalt {w.passengers.total()})")
        if w.passengers.total() == 0:
            print("     ⚠ Inga resenärer — DG kräver minst en")
            warnings += 1
        print(f"     Fordon:      {_human_vehicle(w.vehicle)}")
        tid_parts = []
        if w.departure_time:
            tid_parts.append(f"exakt {w.departure_time} utresa")
        if w.departure_window:
            tid_parts.append(f"utresefönster {w.departure_window}")
        if w.return_time:
            tid_parts.append(f"exakt {w.return_time} retur")
        if w.return_window:
            tid_parts.append(f"returfönster {w.return_window}")
        print(f"     Tidsfilter:  {', '.join(tid_parts) if tid_parts else 'vilken avgång som helst'}")
        print()

    print("=== Notifieringskanaler ===\n")
    n = settings.notifier
    any_notifier = False
    if n.email_enabled:
        any_notifier = True
        _su = n.resolved_smtp_user() or "(ej satt)"
        _to = n.resolved_email_to() or "(ej satt)"
        print(f"  ✓ Email aktiverad: {_su} → {_to}")
        if not n.smtp_user and not os.environ.get(n.smtp_user_env):
            print(f"    ⚠ Varken smtp_user i config eller env {n.smtp_user_env} satt")
        if not n.email_to and not os.environ.get(n.email_to_env):
            print(f"    ⚠ Varken email_to i config eller env {n.email_to_env} satt")
        if not os.environ.get(n.smtp_password_env):
            print(f"    ⚠ Miljövariabeln {n.smtp_password_env} är inte satt just nu")
            print(f"      (måste sättas vid körning, t.ex. i launchd-plisten)")
            warnings += 1
    if n.pushover_enabled:
        any_notifier = True
        print("  ✓ Pushover aktiverad")
    if n.telegram_enabled:
        any_notifier = True
        print("  ✓ Telegram aktiverad")
    if not any_notifier:
        print("  ⚠ Ingen notifierare aktiverad — träffar skrivs bara ut i terminalen")
        warnings += 1
    print()

    print("=== Resultat ===\n")
    if errors == 0 and warnings == 0:
        print("  ✓ Konfigurationen är OK. Kör 'python watch.py' för att starta.")
    elif errors == 0:
        print(f"  ⚠ {warnings} varning(ar) men inga fel. Du kan köra men kolla varningarna ovan.")
    else:
        print(f"  ✗ {errors} fel hittade. Åtgärda dem innan du kör.")
    return 0 if errors == 0 else 1


def test_email_command(path: Path) -> int:
    """Send a test email to verify SMTP setup."""
    settings = load_config(path)
    n = settings.notifier
    if not n.email_enabled:
        print("  ✗ email_enabled är false i config.yaml — sätt true först.")
        return 1
    if not os.environ.get(n.smtp_password_env):
        print(f"  ✗ Miljövariabeln {n.smtp_password_env} är inte satt.")
        print(f"    Kör först: export {n.smtp_password_env}='ditt-app-lösen'")
        return 1
    log = logging.getLogger("gotland_watch")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"[Gotland] Testmejl {ts}"
    body = (
        "Detta är ett testmejl från gotland_watch.\n\n"
        f"Skickat: {ts}\n"
        f"Från: {n.resolved_smtp_user()}\n"
        f"Till: {n.resolved_email_to()}\n\n"
        "Om du ser detta är Gmail-uppsättningen korrekt."
    )
    try:
        _send_email(n, subject, body, log)
        print(f"  ✓ Testmejl skickat till {n.resolved_email_to()}. Kolla inkorgen.")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ Misslyckades skicka mejl: {type(e).__name__}: {e}")
        if "535" in str(e) or "authentication" in str(e).lower():
            print("    Det här ser ut som ett autentiseringsfel.")
            print("    Kolla att SMTP_PASSWORD är ett app-lösenord (16 tecken, inga mellanslag)")
            print("    och inte ditt vanliga Google-lösenord.")
        return 1


def status_command(path: Path) -> int:
    """Print a Swedish status report with history summary."""
    import subprocess
    from collections import Counter
    from datetime import timedelta

    def _ago(td: timedelta) -> str:
        s = td.total_seconds()
        if s < 3600:
            return f"{int(s / 60)} min sedan"
        if s < 86400:
            return f"{int(s / 3600)} timmar sedan"
        return f"{td.days} dagar sedan"

    print("=== gotland_watch status ===\n")

    # 1. launchd
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True,
                           text=True, timeout=3)
        line = next((ln for ln in r.stdout.splitlines() if "gotlandwatch" in ln), None)
        if line:
            parts = line.split(maxsplit=2)
            pid, exit_code = parts[0], parts[1] if len(parts) > 1 else "?"
            pid_info = "vilande" if pid == "-" else f"kör (PID {pid})"
            print(f"  Launchd:  ✓ laddad ({pid_info}), senaste exit {exit_code}")
        else:
            print("  Launchd:  ✗ INTE LADDAD — bevakningen kör inte")
            print("            Fix: launchctl load "
                  "~/Library/LaunchAgents/com.gotlandwatch.plist")
    except FileNotFoundError:
        print("  Launchd:  (inte Mac?)")
    except Exception as e:  # noqa: BLE001
        print(f"  Launchd:  (fel: {e})")

    # 2. Config: what's being watched
    settings = None
    try:
        settings = load_config(path)
    except Exception as e:  # noqa: BLE001
        print(f"\n  ⚠ Kunde inte läsa config: {e}")

    if settings and settings.watches:
        print("\n  Bevakar:")
        today = datetime.now().date()
        for w in settings.watches:
            try:
                out_dt = datetime.strptime(w.outbound_date, "%Y-%m-%d").date()
                days = (out_dt - today).days
                days_str = ("idag" if days == 0
                            else f"om {days} d" if days > 0
                            else f"{-days} d sedan")
            except Exception:  # noqa: BLE001
                days_str = "?"
            target = w.departure_time or w.departure_window or "valfri avgång"
            print(f"    '{w.name}': {w.outbound_date} ({days_str}), tid: {target}")

    # 3. Read log
    log_text = ""
    for cand in ("watch.err", "watch.log"):
        p = Path(cand)
        if p.exists() and p.stat().st_size > 0:
            try:
                log_text = p.read_text(errors="replace")
                if log_text.strip():
                    break
            except Exception:  # noqa: BLE001
                pass

    if not log_text:
        print("\n  ⚠ Ingen logg-data. Vänta tills första launchd-körningen "
              "och kom tillbaka.")
        return 0

    # 4. Parse runs from log
    run_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO === Watch: (.+?) ==="
    )
    all_matches = list(run_re.finditer(log_text))
    if not all_matches:
        print("\n  ⚠ Inga körningar hittades i loggen än.")
        return 0

    runs = []
    for i, m in enumerate(all_matches):
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        name = m.group(2)
        start = m.end()
        end = all_matches[i + 1].start() if i + 1 < len(all_matches) else len(log_text)
        block = log_text[start:end]
        parsed = "Parsed departures for" in block or "hit(s) for" in block
        errored = "ERROR" in block
        runs.append({"ts": ts, "name": name, "block": block,
                     "success": parsed and not errored, "errored": errored})

    # 5. Overall history summary
    now = datetime.now()
    first_run, last_run = runs[0]["ts"], runs[-1]["ts"]
    total = len(runs)
    ok_count = sum(1 for r in runs if r["success"])
    err_count = sum(1 for r in runs if r["errored"])
    last_mark = "✓" if runs[-1]["success"] else "✗"

    print("\n  Bevakningshistorik:")
    print(f"    Sedan:            {first_run.strftime('%Y-%m-%d %H:%M')} "
          f"({_ago(now - first_run)})")
    print(f"    Antal kontroller: {total} ({ok_count} ok, {err_count} fel)")
    print(f"    Senaste kontroll: {last_run.strftime('%Y-%m-%d %H:%M')} "
          f"({_ago(now - last_run)}) {last_mark}")

    # 6. Per-watch: status for the target departure
    if settings:
        for w in settings.watches:
            watch_runs = [r for r in runs if r["name"] == w.name and r["success"]]
            if not watch_runs or not w.departure_time:
                continue
            target_re = re.compile(
                rf"\[utresa\]\s+{re.escape(w.departure_time)}\s+->\s+\S+\s+(\S+)"
            )
            statuses = []
            last_avail = None
            for r in watch_runs:
                mm = target_re.search(r["block"])
                if mm:
                    status = mm.group(1)
                    statuses.append(status)
                    if status.lower() == "tillgänglig":
                        last_avail = r["ts"]
            if not statuses:
                print(f"\n  Avgång {w.departure_time}: aldrig sedd i loggen "
                      "(kanske parsning-problem?)")
                continue
            print(f"\n  Avgång {w.departure_time}:")
            print(f"    Sedd vid kontroller: {len(statuses)} "
                  f"av {len(watch_runs)} lyckade")
            for status, n in Counter(statuses).most_common():
                print(f"    {status}: {n} gånger")
            print(f"    Senast tillgänglig: "
                  + (last_avail.strftime('%Y-%m-%d %H:%M') if last_avail else "aldrig"))

    # 7. Slutsats
    print("\n  Slutsats:")
    last_age_min = int((now - last_run).total_seconds() / 60)
    if last_age_min > 60:
        print(f"    ⚠ Senaste kontroll var {last_age_min} min sedan — kolla launchd.")
    elif err_count > total * 0.3:
        print(f"    ⚠ {err_count}/{total} fel-körningar. Kolla watch.err.")
    else:
        print("    ✓ Bevakningen kör som den ska.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Destination Gotland availability watcher")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--watch", default=None)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--inspect", action="store_true",
                        help="Just open the home page, dump HTML, and exit")
    parser.add_argument("--validate", action="store_true",
                        help="Validate config.yaml and print a human-readable summary, then exit")
    parser.add_argument("--test-email", action="store_true",
                        help="Send a test email to verify SMTP setup, then exit")
    parser.add_argument("--status", action="store_true",
                        help="Print a status report (is the watcher healthy?), then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("gotland_watch")

    if args.validate:
        return validate_config_command(Path(args.config))
    if args.test_email:
        return test_email_command(Path(args.config))
    if args.status:
        return status_command(Path(args.config))

    settings = load_config(Path(args.config))
    if args.no_headless:
        settings.headless = False
    debug_dir = Path("debug") if args.debug else None

    if args.inspect:
        inspect_home(settings, log)
        return 0
    if args.daemon:
        while True:
            try:
                run_once(settings, debug_dir, args.watch, log)
            except Exception as e:  # noqa: BLE001
                log.exception("Cycle failed: %s", e)
            log.info("Sleeping %d s", settings.interval_seconds)
            time.sleep(settings.interval_seconds)
    else:
        run_once(settings, debug_dir, args.watch, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
