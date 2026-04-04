"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — economic_calendar.py (v6.4)                   ║
║                                                                          ║
║   FIX v6.4: HTTP 404 en nextweek.json → log.debug (no warning)        ║
║   El 404 en ff_calendar_nextweek.json es NORMAL los lunes/martes       ║
║   porque ForexFactory no lo publica hasta el miércoles. Generar        ║
║   11 warnings por sesión contamina el log innecesariamente.            ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import logging
import requests
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set, Tuple

import config as cfg

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 15


def _sym(base: str) -> str:
    """Retorna el nombre completo del símbolo según el broker configurado."""
    if base in cfg._NO_SUFFIX:
        return base
    return f"{base}{cfg.BROKER_SUFFIX}"


# ════════════════════════════════════════════════════════════════
#  MAPA SÍMBOLO → DIVISAS AFECTADAS POR EL CALENDARIO
# ════════════════════════════════════════════════════════════════

SYMBOL_CURRENCIES: Dict[str, List[str]] = {
    _sym("XAUUSD"):  ["USD"],
    _sym("US500"):   ["USD"],
    _sym("EURUSD"):  ["EUR", "USD"],
    _sym("GBPUSD"):  ["GBP", "USD"],
    _sym("USDJPY"):  ["USD", "JPY"],
    _sym("GBPJPY"):  ["GBP", "JPY"],
    _sym("XAGUSD"):  ["USD"],
    _sym("USOIL"):   ["USD"],
    _sym("USTEC"):   ["USD"],
    _sym("DE40"):    ["EUR"],
    _sym("EURJPY"):  ["EUR", "JPY"],
    _sym("BTCUSD"):  ["USD"],
}

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

HIGH_IMPACT_TITLES = [
    "Non-Farm", "NFP", "Nonfarm",
    "Fed Funds Rate", "Interest Rate Decision", "Rate Decision",
    "FOMC", "Federal Open Market",
    "CPI", "Consumer Price Index",
    "PPI", "Producer Price",
    "GDP", "Gross Domestic Product",
    "PCE", "Personal Consumption",
    "ISM Manufacturing", "ISM Services",
    "Retail Sales",
    "Unemployment Rate", "Jobless Claims",
    "ECB", "European Central Bank Rate",
    "BOE", "Bank of England Rate",
    "BOJ", "Bank of Japan Rate",
    "RBA", "Reserve Bank",
    "Inflation",
    "Payrolls",
    "EIA Crude", "EIA Petroleum",
]


# ════════════════════════════════════════════════════════════════
#  DATACLASSES
# ════════════════════════════════════════════════════════════════

@dataclass
class CalendarEvent:
    """Un evento económico del calendario."""
    title:        str
    currency:     str
    datetime_utc: datetime
    impact:       str
    forecast:     str = ""
    previous:     str = ""
    actual:       str = ""

    @property
    def event_id(self) -> str:
        ts = self.datetime_utc.strftime("%Y%m%d_%H%M")
        return f"{self.currency}_{ts}_{self.title[:25].replace(' ', '_')}"

    @property
    def is_high_impact(self) -> bool:
        return self.impact.lower() in ("high", "alto", "3")

    def minutes_until(self) -> float:
        now = datetime.now(timezone.utc)
        return (self.datetime_utc - now).total_seconds() / 60

    def time_label(self) -> str:
        mins = self.minutes_until()
        if mins > 60:
            return f"en {int(mins/60)}h {int(mins%60)}min"
        elif mins > 1:
            return f"en {int(mins)}min"
        elif mins > -1:
            return "¡AHORA!"
        else:
            return f"hace {int(abs(mins))}min"

    def __str__(self) -> str:
        return f"[{self.currency}] {self.title} {self.time_label()} ({self.impact})"


@dataclass
class CalendarStatus:
    """Estado actual del calendario para dashboard y Telegram."""
    events_next_hour:   List[CalendarEvent] = field(default_factory=list)
    events_active_pause: List[CalendarEvent] = field(default_factory=list)
    paused_currencies:  Set[str]             = field(default_factory=set)
    next_high_impact:   Optional[CalendarEvent] = None
    last_updated:       str = ""

    def summary_line(self) -> str:
        if self.events_active_pause:
            ev = self.events_active_pause[0]
            return f"🚨 PAUSA ACTIVA: [{ev.currency}] {ev.title[:35]} ({ev.time_label()})"
        if self.next_high_impact:
            return f"📅 Próximo alto impacto: [{self.next_high_impact.currency}] {self.next_high_impact.title[:35]} {self.next_high_impact.time_label()}"
        return "📅 Sin eventos de alto impacto próximos (2h)"


# ════════════════════════════════════════════════════════════════
#  CLASE PRINCIPAL — EconomicCalendar
# ════════════════════════════════════════════════════════════════

class EconomicCalendar:

    def __init__(self):
        self._events: List[CalendarEvent] = []
        self._last_fetch: Optional[datetime] = None
        self._notified_events: Set[str] = set()
        self._fetch_errors: int = 0
        self._max_errors: int = 5

    def _needs_refresh(self) -> bool:
        if self._last_fetch is None:
            return True
        age_hours = (datetime.now(timezone.utc) - self._last_fetch).total_seconds() / 3600
        return age_hours >= cfg.CALENDAR_REFRESH_HOURS

    def refresh(self, force: bool = False):
        if not force and not self._needs_refresh():
            return

        log.info("[calendar] 📅 Actualizando calendario económico...")
        new_events = self._fetch_forexfactory()

        if new_events:
            self._events = new_events
            self._last_fetch = datetime.now(timezone.utc)
            self._fetch_errors = 0
            log.info(f"[calendar] ✅ {len(self._events)} eventos cargados (próximos 14 días)")
        else:
            self._fetch_errors += 1
            log.warning(f"[calendar] ⚠️ Sin datos nuevos (error #{self._fetch_errors}) — manteniendo caché")

        self._clean_old_notifications()

    def _fetch_forexfactory(self) -> List[CalendarEvent]:
        """
        FIX v6.4: HTTP 404 en nextweek.json → log.debug (no warning).
        El 404 en nextweek es NORMAL los lunes/martes — ForexFactory no
        publica la semana siguiente hasta el miércoles. Antes generaba
        11+ warnings por sesión contaminando el log sin valor.
        """
        all_events: List[CalendarEvent] = []

        for url in FF_URLS:
            try:
                r = requests.get(
                    url,
                    timeout=HTTP_TIMEOUT,
                    headers={"User-Agent": "ZAR-Bot/6.4 Calendar Fetcher"},
                )
                if r.status_code == 404:
                    # FIX: 404 en nextweek es normal → debug no warning
                    log.debug(f"[calendar] 404 en {url.split('/')[-1]} (normal lunes/martes)")
                    continue
                elif r.status_code != 200:
                    log.warning(f"[calendar] HTTP {r.status_code} en {url}")
                    continue

                data = r.json()
                if not isinstance(data, list):
                    continue

                parsed = 0
                for item in data:
                    ev = self._parse_event(item)
                    if ev is not None:
                        all_events.append(ev)
                        parsed += 1

                log.debug(f"[calendar] {url.split('/')[-1]}: {parsed} eventos parseados")

            except requests.Timeout:
                log.warning(f"[calendar] Timeout al conectar con {url}")
            except Exception as e:
                log.warning(f"[calendar] Error en {url}: {e}")

        # Eliminar duplicados
        seen_ids: Set[str] = set()
        unique_events = []
        for ev in all_events:
            if ev.event_id not in seen_ids:
                seen_ids.add(ev.event_id)
                unique_events.append(ev)

        unique_events.sort(key=lambda e: e.datetime_utc)

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        unique_events = [e for e in unique_events if e.datetime_utc >= cutoff]

        return unique_events

    def _parse_event(self, item: dict) -> Optional[CalendarEvent]:
        try:
            title    = str(item.get("title", "")).strip()
            currency = str(item.get("country", item.get("currency", ""))).upper().strip()
            impact   = str(item.get("impact", "Low")).strip()
            date_str = str(item.get("date", "")).strip()

            if not title or not currency or not date_str:
                return None

            if cfg.CALENDAR_HIGH_IMPACT_ONLY:
                if not self._is_high_impact(impact, title):
                    return None

            dt_utc = self._parse_datetime(date_str)
            if dt_utc is None:
                return None

            return CalendarEvent(
                title=title,
                currency=currency,
                datetime_utc=dt_utc,
                impact=impact,
                forecast=str(item.get("forecast", "")),
                previous=str(item.get("previous", "")),
                actual=str(item.get("actual", "")),
            )

        except Exception as e:
            log.debug(f"[calendar] Error parseando item: {e}")
            return None

    def _is_high_impact(self, impact: str, title: str) -> bool:
        if impact.lower() in ("high", "3", "red"):
            return True
        title_lower = title.lower()
        return any(kw.lower() in title_lower for kw in HIGH_IMPACT_TITLES)

    def _parse_datetime(self, date_str: str) -> Optional[datetime]:
        if not date_str:
            return None

        try:
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc)
            else:
                return dt.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            pass

        formats = [
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%m/%d/%Y %H:%M",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str[:len(fmt)], fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        log.debug(f"[calendar] No se pudo parsear: {date_str!r}")
        return None

    def get_events_for_symbol(
        self,
        symbol: str,
        sym_cfg: dict,
        minutes_ahead: int = 120,
    ) -> List[CalendarEvent]:
        self.refresh()
        currencies = SYMBOL_CURRENCIES.get(
            symbol,
            sym_cfg.get("currencies", ["USD"])
        )
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=cfg.CALENDAR_RESUME_MINUTES_AFTER)
        window_end   = now + timedelta(minutes=minutes_ahead)

        result = []
        for ev in self._events:
            if ev.currency in currencies:
                if window_start <= ev.datetime_utc <= window_end:
                    result.append(ev)

        return sorted(result, key=lambda e: e.datetime_utc)

    def should_pause_symbol(
        self,
        symbol: str,
        sym_cfg: dict,
    ) -> Tuple[bool, str, Optional[CalendarEvent], bool]:
        self.refresh()

        currencies = SYMBOL_CURRENCIES.get(
            symbol,
            sym_cfg.get("currencies", ["USD"])
        )

        pause_before = cfg.CALENDAR_PAUSE_MINUTES_BEFORE
        resume_after = cfg.CALENDAR_RESUME_MINUTES_AFTER

        now_utc = datetime.now(timezone.utc)

        for ev in self._events:
            if ev.currency not in currencies:
                continue
            if not ev.is_high_impact:
                continue

            mins = ev.minutes_until()

            if -resume_after <= mins <= pause_before:
                if mins > 0:
                    reason = (
                        f"⚡ EVENTO INMINENTE en {int(mins)}min: "
                        f"[{ev.currency}] {ev.title}"
                        + (f" | Forecast: {ev.forecast}" if ev.forecast else "")
                    )
                elif mins > -1:
                    reason = f"🚨 EVENTO ACTIVO AHORA: [{ev.currency}] {ev.title}"
                else:
                    reason = (
                        f"⏳ POST-EVENTO ({int(abs(mins))}min después): "
                        f"[{ev.currency}] {ev.title} — esperando estabilización"
                    )

                already_notified = ev.event_id in self._notified_events
                return True, reason, ev, already_notified

        return False, "", None, False

    def mark_notified(self, event: CalendarEvent):
        self._notified_events.add(event.event_id)
        log.debug(f"[calendar] Marcado notificado: {event.event_id}")

    def _clean_old_notifications(self):
        if not self._events:
            return
        active_ids = {ev.event_id for ev in self._events if ev.minutes_until() > -120}
        removed = self._notified_events - active_ids
        self._notified_events -= removed
        if removed:
            log.debug(f"[calendar] Limpiadas {len(removed)} notificaciones antiguas")

    def get_status(self) -> CalendarStatus:
        self.refresh()

        pause_before = cfg.CALENDAR_PAUSE_MINUTES_BEFORE
        resume_after = cfg.CALENDAR_RESUME_MINUTES_AFTER

        events_active_pause: List[CalendarEvent] = []
        events_next_hour: List[CalendarEvent] = []
        paused_currencies: Set[str] = set()

        for ev in self._events:
            mins = ev.minutes_until()
            if -resume_after <= mins <= pause_before and ev.is_high_impact:
                events_active_pause.append(ev)
                paused_currencies.add(ev.currency)
            elif 0 < mins <= 60:
                events_next_hour.append(ev)

        future_high = [e for e in self._events if e.minutes_until() > 0 and e.is_high_impact]
        next_high = min(future_high, key=lambda e: e.minutes_until()) if future_high else None

        last_updated = (
            self._last_fetch.strftime("%H:%M UTC") if self._last_fetch else "No cargado"
        )

        return CalendarStatus(
            events_next_hour=sorted(events_next_hour, key=lambda e: e.datetime_utc),
            events_active_pause=events_active_pause,
            paused_currencies=paused_currencies,
            next_high_impact=next_high,
            last_updated=last_updated,
        )

    def format_for_dashboard(self) -> str:
        status = self.get_status()
        lines = []

        if status.events_active_pause:
            for ev in status.events_active_pause:
                lines.append(f"  🚨 PAUSA [{ev.currency}] {ev.title[:38]} ({ev.time_label()})")
        elif status.next_high_impact:
            ev = status.next_high_impact
            lines.append(f"  📅 [{ev.currency}] {ev.title[:40]} → {ev.time_label()}")

        for ev in status.events_next_hour[:4]:
            mins = ev.minutes_until()
            icon = "⚡" if mins <= 5 else ("⏰" if mins <= 15 else "📍")
            lines.append(
                f"  {icon} {ev.currency}: {ev.title[:32]} "
                f"({int(mins)}min) [{ev.impact}]"
            )

        if status.paused_currencies:
            lines.append(
                f"  🔴 Divisas pausadas: {', '.join(sorted(status.paused_currencies))}"
            )

        lines.append(f"  🔄 Actualizado: {status.last_updated}")

        return "\n".join(lines) if lines else "  📅 Sin eventos de alto impacto próximos (1h)"

    def format_for_telegram(self, symbol: str, event: CalendarEvent) -> str:
        mins = event.minutes_until()
        if mins > 0:
            timing = f"en <b>{int(mins)} minuto{'s' if int(mins) != 1 else ''}</b>"
        elif mins > -1:
            timing = "<b>AHORA MISMO</b>"
        else:
            timing = f"hace <b>{int(abs(mins))} minutos</b> — esperando estabilización"

        lines = [
            f"📅 <b>PAUSA CALENDARIO — {symbol}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"⚡ Evento: <b>{event.title}</b>",
            f"🌍 Divisa: <code>{event.currency}</code>",
            f"⏰ Ocurre: {timing}",
            f"📊 Impacto: <code>{event.impact}</code>",
        ]
        if event.forecast:
            lines.append(f"📈 Forecast: <code>{event.forecast}</code>")
        if event.previous:
            lines.append(f"📉 Anterior: <code>{event.previous}</code>")
        lines += [
            "",
            f"🛑 Trading pausado por <b>{cfg.CALENDAR_RESUME_MINUTES_AFTER} min</b>",
            "✅ Reanuda automáticamente después del evento.",
        ]
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  SINGLETON GLOBAL
# ════════════════════════════════════════════════════════════════

calendar = EconomicCalendar()