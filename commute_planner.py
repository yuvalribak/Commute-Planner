#!/usr/bin/env python3
"""
Weather & Commute Planner
-------------------------
Every weekday morning: plans your commute to arrive by a set time
(from config.yaml), fetches the Met Office forecast for leaving and arriving,
plans the journey with the TfL Journey Planner, checks line status, and emails you
a summary.

"""

import argparse
import os
import smtplib
import ssl
import sys
from datetime import datetime, date, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

LONDON = ZoneInfo("Europe/London")
HERE = Path(__file__).resolve().parent

MET_OFFICE_URL = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly"
TFL_BASE = "https://api.tfl.gov.uk"

# Met Office "significant weather" codes
WEATHER_CODES = {
    -1: "Trace rain", 0: "Clear night", 1: "Sunny", 2: "Partly cloudy",
    3: "Partly cloudy", 5: "Mist", 6: "Fog", 7: "Cloudy", 8: "Overcast",
    9: "Light rain shower", 10: "Light rain shower", 11: "Drizzle",
    12: "Light rain", 13: "Heavy rain shower", 14: "Heavy rain shower",
    15: "Heavy rain", 16: "Sleet shower", 17: "Sleet shower", 18: "Sleet",
    19: "Hail shower", 20: "Hail shower", 21: "Hail",
    22: "Light snow shower", 23: "Light snow shower", 24: "Light snow",
    25: "Heavy snow shower", 26: "Heavy snow shower", 27: "Heavy snow",
    28: "Thunder shower", 29: "Thunder shower", 30: "Thunder",
}

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]



# Config

def load_config() -> dict:
    # settings.env 
    load_dotenv(HERE / "settings.env")
    load_dotenv(HERE / ".env")  
    with open(HERE / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    required_env = ["MET_OFFICE_API_KEY", "SMTP_HOST", "SMTP_USER",
                    "SMTP_PASSWORD", "EMAIL_TO"]
    missing = [k for k in required_env if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)} (fill them in settings.env)")
    return cfg



# Weather (Met Office Weather DataHub - Site Specific)

_weather_cache: dict = {}


def get_hourly_forecast(lat: float, lon: float) -> list[dict]:
    """Return the hourly timeSeries for a location (cached per run)."""
    key = (round(lat, 4), round(lon, 4))
    if key in _weather_cache:
        return _weather_cache[key]

    resp = requests.get(
        MET_OFFICE_URL,
        headers={"apikey": os.environ["MET_OFFICE_API_KEY"],
                 "accept": "application/json"},
        params={"latitude": lat, "longitude": lon,
                "excludeParameterMetadata": "true",
                "includeLocationName": "true"},
        timeout=30,
    )
    resp.raise_for_status()
    series = resp.json()["features"][0]["properties"]["timeSeries"]
    _weather_cache[key] = series
    return series


def parse_met_time(s: str) -> datetime:
    # Met Office format: "2026-09-23T06:00Z"
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def forecast_at(lat: float, lon: float, when: datetime) -> dict | None:
    """Closest hourly forecast entry to `when`."""
    series = get_hourly_forecast(lat, lon)
    target = when.astimezone(timezone.utc)
    best = min(series, key=lambda e: abs(parse_met_time(e["time"]) - target))
    if abs(parse_met_time(best["time"]) - target) > timedelta(hours=2):
        return None
    return best


def forecast_range(lat: float, lon: float, start: datetime, end: datetime) -> list[dict]:
    series = get_hourly_forecast(lat, lon)
    s, e = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    return [x for x in series if s <= parse_met_time(x["time"]) <= e]


def describe_weather(w: dict) -> str:
    desc = WEATHER_CODES.get(w.get("significantWeatherCode"), "Unknown")
    temp = w.get("screenTemperature")
    feels = w.get("feelsLikeTemperature")
    rain = w.get("probOfPrecipitation", 0)
    wind = w.get("windSpeed10m", 0) * 2.237      # m/s -> mph
    gust = w.get("windGustSpeed10m", 0) * 2.237
    return (f"{desc}, {temp:.0f}°C (feels {feels:.0f}°C), "
            f"{rain}% chance of rain, wind {wind:.0f} mph (gusts {gust:.0f})")


def weather_advice(entries: list[dict], thresholds: dict) -> list[str]:
    """Simple practical tips based on the day's forecast."""
    if not entries:
        return []
    tips = []
    max_rain = max(e.get("probOfPrecipitation", 0) for e in entries)
    min_feels = min(e.get("feelsLikeTemperature", 99) for e in entries)
    max_temp = max(e.get("screenTemperature", -99) for e in entries)
    max_gust = max(e.get("windGustSpeed10m", 0) for e in entries) * 2.237
    max_uv = max(e.get("uvIndex", 0) for e in entries)
    codes = {e.get("significantWeatherCode") for e in entries}

    if max_rain >= thresholds.get("umbrella_rain_pct", 40):
        tips.append(f"☔ Take an umbrella – up to {max_rain}% chance of rain.")
    if min_feels <= thresholds.get("coat_feels_like_c", 10):
        tips.append(f"🧥 Wrap up – feels as cold as {min_feels:.0f}°C.")
    if max_temp >= thresholds.get("hot_c", 26):
        tips.append(f"🥵 Hot day (up to {max_temp:.0f}°C) – carry water; trains may be stuffy.")
    if max_gust >= thresholds.get("windy_gust_mph", 35):
        tips.append(f"💨 Gusty – up to {max_gust:.0f} mph. Watch for disruption on exposed lines.")
    if max_uv >= thresholds.get("uv_index", 6):
        tips.append(f"😎 High UV (index {max_uv}) – sunscreen if you're outdoors.")
    if codes & {5, 6}:
        tips.append("🌫️ Mist/fog expected – allow extra time.")
    if codes & set(range(16, 28)):
        tips.append("❄️ Snow/sleet/hail forecast – expect delays.")
    if codes & {28, 29, 30}:
        tips.append("⛈️ Thunderstorms possible.")
    return tips



# Commute (TfL Unified API)

def tfl_params(extra: dict | None = None) -> dict:
    p = {}
    key = (os.getenv("TFL_APP_KEY") or "").strip()
    if key and not key.startswith("your-"):  # ignore blank or placeholder keys
        p["app_key"] = key
    if extra:
        p.update(extra)
    return p


def plan_journey(origin: dict, dest: dict, arrive_by: datetime, modes: list[str]) -> dict | None:
    """TfL journey that gets you there by `arrive_by`, leaving as late as possible."""
    frm = f"{origin['lat']},{origin['lon']}"
    to = f"{dest['lat']},{dest['lon']}"
    params = tfl_params({
        "date": arrive_by.strftime("%Y%m%d"),
        "time": arrive_by.strftime("%H%M"),
        "timeIs": "Arriving",
        "journeyPreference": "LeastTime",
        "mode": ",".join(modes),
        "walkingSpeed": "Average",
    })
    resp = requests.get(f"{TFL_BASE}/Journey/JourneyResults/{frm}/to/{to}",
                        params=params, timeout=30)
    if resp.status_code != 200:
        return {"error": f"TfL journey planner returned HTTP {resp.status_code}"}
    journeys = resp.json().get("journeys", [])
    if not journeys:
        return {"error": "No journeys found"}

    # Only keep journeys that genuinely arrive on time, then pick the latest departure
    target = arrive_by.replace(tzinfo=None)
    on_time = [j for j in journeys
               if datetime.fromisoformat(j["arrivalDateTime"]) <= target] or journeys
    return max(on_time, key=lambda j: datetime.fromisoformat(j["startDateTime"]))


def summarise_journey(j: dict) -> dict:
    legs, lines, disruptions = [], set(), []
    for leg in j.get("legs", []):
        mode = leg.get("mode", {}).get("id", "")
        summary = leg.get("instruction", {}).get("summary", "")
        mins = leg.get("duration", 0)
        if mode == "walking" and mins <= 2:
            continue  # skip trivial walks
        legs.append(f"{summary} ({mins} min)")
        for opt in leg.get("routeOptions", []):
            ident = opt.get("lineIdentifier") or {}
            if ident.get("id") and mode != "walking":
                lines.add(ident["id"])
        if leg.get("isDisrupted"):
            for d in leg.get("disruptions", []):
                desc = (d.get("description") or "").strip()
                if desc and desc not in disruptions:
                    disruptions.append(desc)

    fare = None
    if j.get("fare", {}).get("totalCost"):
        fare = j["fare"]["totalCost"] / 100

    return {
        "duration": j.get("duration"),
        "depart": datetime.fromisoformat(j["startDateTime"]).strftime("%H:%M"),
        "arrive": datetime.fromisoformat(j["arrivalDateTime"]).strftime("%H:%M"),
        "legs": legs,
        "lines": sorted(lines),
        "disruptions": disruptions,
        "fare": fare,
    }


def line_statuses(line_ids: list[str]) -> list[dict]:
    """Current status for the given line ids; returns only non-'Good Service' lines."""
    if not line_ids:
        return []
    resp = requests.get(f"{TFL_BASE}/Line/{','.join(line_ids)}/Status",
                        params=tfl_params(), timeout=30)
    if resp.status_code != 200:
        return [{"name": "Line status", "status": f"unavailable (HTTP {resp.status_code})", "reason": ""}]
    problems = []
    for line in resp.json():
        for st in line.get("lineStatuses", []):
            if st.get("statusSeverity") != 10:  # 10 = Good Service
                problems.append({
                    "name": line.get("name"),
                    "status": st.get("statusSeverityDescription"),
                    "reason": (st.get("reason") or "").strip(),
                })
    return problems



# Build the report

def build_report(cfg: dict, day: date) -> tuple[str, str, str] | None:
    commute = cfg["commute"]
    if DAY_NAMES[day.weekday()] not in [d.lower() for d in commute.get("days", [])]:
        return None  # not a commuting day

    locations = cfg["locations"]
    origin = locations[commute["from"]]
    dest = locations[commute["to"]]
    modes = cfg.get("modes", ["tube", "bus", "overground", "elizabeth-line",
                              "dlr", "national-rail", "walking"])
    thresholds = cfg.get("thresholds", {})
    buffer = int(commute.get("buffer_minutes", 0))

    hh, mm = map(int, commute["arrive_by"].split(":"))
    must_arrive = datetime.combine(day, datetime.min.time(), LONDON).replace(hour=hh, minute=mm)
    plan_arrive = must_arrive - timedelta(minutes=buffer)

    title = f"{origin['label']} → {dest['label']} (arrive by {commute['arrive_by']})"
    section = {"title": title}
    leave_by = None
    lines: list[str] = []

    # Plan the journey for leaving time
    try:
        j = plan_journey(origin, dest, plan_arrive, modes)
        if j and "error" not in j:
            s = summarise_journey(j)
            section["journey"] = s
            lines = s["lines"]
            leave_by = s["depart"]
            dep_dt = datetime.fromisoformat(j["startDateTime"]).replace(tzinfo=LONDON)
            arr_dt = datetime.fromisoformat(j["arrivalDateTime"]).replace(tzinfo=LONDON)
        else:
            section["error"] = (j or {}).get("error", "Journey planner failed")
            dep_dt, arr_dt = plan_arrive - timedelta(hours=1), plan_arrive
    except requests.RequestException as e:
        section["error"] = f"TfL unavailable: {e}"
        dep_dt, arr_dt = plan_arrive - timedelta(hours=1), plan_arrive

    # Weather when leaving and when arriving
    overview = None
    try:
        w_dep = forecast_at(origin["lat"], origin["lon"], dep_dt)
        section["weather"] = describe_weather(w_dep) if w_dep else "No forecast for this time"
        w_arr = forecast_at(dest["lat"], dest["lon"], arr_dt)
        if w_arr:
            section["arrival_weather"] = describe_weather(w_arr)

        # Whole-day overview at the office (08:00–19:00) for the range and tips
        day_start = datetime.combine(day, datetime.min.time(), LONDON).replace(hour=8)
        day_entries = forecast_range(dest["lat"], dest["lon"], day_start, day_start.replace(hour=19))
        if day_entries:
            temps = [e["screenTemperature"] for e in day_entries]
            overview = {"range": f"{min(temps):.0f}–{max(temps):.0f}°C",
                        "tips": weather_advice(day_entries, thresholds)}
    except requests.RequestException as e:
        section["weather"] = f"Met Office unavailable: {e}"

    try:
        problems = line_statuses(lines)
    except requests.RequestException as e:
        problems = [{"name": "Line status", "status": f"unavailable ({e})", "reason": ""}]

    subject = f"🚆 {day.strftime('%a %d %b')}"
    if leave_by:
        subject += f": leave by {leave_by}"
    if overview:
        subject += f" · {overview['range']}"
    if problems:
        subject += f" · ⚠️ {len(problems)} line issue(s)"
    elif lines:
        subject += " · ✅ Good service"

    text = render_text(day, overview, [section], problems, leave_by)
    html = render_html(day, overview, [section], problems, leave_by)
    return subject, text, html



# Rendering

def render_text(day, overview, sections, problems, leave_by=None) -> str:
    out = [f"Commute summary for {day.strftime('%A %d %B %Y')}", ""]
    if leave_by:
        out += [f">>> LEAVE HOME BY {leave_by} <<<", ""]
    if overview:
        out.append(f"Today: {overview['range']}")
        out += [f"  {t}" for t in overview["tips"]]
        out.append("")
    if problems:
        out.append("LINE STATUS ISSUES")
        for p in problems:
            out.append(f"  {p['name']}: {p['status']}")
            if p["reason"]:
                out.append(f"    {p['reason']}")
        out.append("")
    for s in sections:
        out.append(s["title"].upper())
        if "weather" in s:
            out.append(f"  Weather at departure: {s['weather']}")
        if "journey" in s:
            j = s["journey"]
            out.append(f"  Journey: {j['depart']} → {j['arrive']} ({j['duration']} min)")
            out += [f"    • {leg}" for leg in j["legs"]]
            if j["fare"]:
                out.append(f"  Fare: £{j['fare']:.2f}")
            for d in j["disruptions"]:
                out.append(f"  ⚠️ {d}")
        if "arrival_weather" in s:
            out.append(f"  Weather on arrival: {s['arrival_weather']}")
        if "error" in s:
            out.append(f"  ⚠️ {s['error']}")
        out.append("")
    return "\n".join(out)


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_html(day, overview, sections, problems, leave_by=None) -> str:
    h = ['<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:620px;color:#222">',
         f'<h2 style="margin-bottom:4px">{day.strftime("%A %d %B")}</h2>']
    if leave_by:
        h.append(f'<p style="font-size:22px;font-weight:bold;margin:4px 0 8px">🚪 Leave home by {leave_by}</p>')
    if overview:
        h.append(f'<p style="font-size:18px;margin-top:0">🌡️ {overview["range"]}</p>')
        if overview["tips"]:
            h.append("<ul>" + "".join(f"<li>{esc(t)}</li>" for t in overview["tips"]) + "</ul>")
    if problems:
        h.append('<div style="background:#fff3cd;border-left:4px solid #e0a800;padding:10px 14px;margin:12px 0">')
        h.append("<strong>⚠️ Line status</strong>")
        for p in problems:
            h.append(f"<p style='margin:6px 0'><b>{esc(p['name'])}</b>: {esc(p['status'])}"
                     f"<br><span style='font-size:13px;color:#555'>{esc(p['reason'])}</span></p>")
        h.append("</div>")
    for s in sections:
        h.append('<div style="border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:12px 0">')
        h.append(f'<h3 style="margin:0 0 8px">{esc(s["title"])}</h3>')
        if "weather" in s:
            h.append(f"<p style='margin:4px 0'>🌦️ <b>Leaving:</b> {esc(s['weather'])}</p>")
        if "journey" in s:
            j = s["journey"]
            h.append(f"<p style='margin:4px 0'>🚇 <b>{j['depart']} → {j['arrive']}</b> ({j['duration']} min)"
                     + (f" · £{j['fare']:.2f}" if j["fare"] else "") + "</p>")
            h.append("<ol style='margin:4px 0 4px 20px;padding:0'>"
                     + "".join(f"<li>{esc(l)}</li>" for l in j["legs"]) + "</ol>")
            for d in j["disruptions"]:
                h.append(f"<p style='color:#b00;font-size:13px;margin:4px 0'>⚠️ {esc(d)}</p>")
        if "arrival_weather" in s:
            h.append(f"<p style='margin:4px 0'>📍 <b>Arriving:</b> {esc(s['arrival_weather'])}</p>")
        if "error" in s:
            h.append(f"<p style='color:#b00'>⚠️ {esc(s['error'])}</p>")
        h.append("</div>")
    h.append('<p style="font-size:11px;color:#888">Weather: Met Office. '
             'Journeys: Powered by TfL Open Data.</p></div>')
    return "\n".join(h)


# Email

def send_email(subject: str, text: str, html: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_FROM", os.environ["SMTP_USER"])
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    else:  # e.g. 587 with STARTTLS
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)



def main() -> None:
    ap = argparse.ArgumentParser(description="Weather & commute morning email")
    ap.add_argument("--dry-run", action="store_true", help="print instead of emailing")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    cfg = load_config()
    day = date.fromisoformat(args.date) if args.date else datetime.now(LONDON).date()

    report = build_report(cfg, day)
    if report is None:
        print(f"No journeys configured for {DAY_NAMES[day.weekday()]} – nothing to send.")
        return

    subject, text, html = report
    if args.dry_run:
        print(f"Subject: {subject}\n\n{text}")
    else:
        send_email(subject, text, html)
        print(f"Sent: {subject}")


if __name__ == "__main__":
    main()