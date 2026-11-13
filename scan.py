#!/usr/bin/env python3
"""
Circuit Watch scanner.

Downloads the daily price files (bhavcopy) that NSE and BSE publish for every
listed stock, finds stocks that have closed at their upper price band session
after session, and scores warning signs that such a run is breaking.

It also replays the same rules over the saved history to measure how often each
warning was followed by a real drop, and writes everything to site/data.json
for the web page to display.

    python scan.py                       # normal run, writes site/data.json
    python scan.py --days 120            # use less history
    python scan.py --offline-dir DIR     # read exchange files from a folder (testing)

Needs: Python 3.9+, pandas, numpy.
"""

import argparse
import http.cookiejar
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import warnings
import zipfile
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)

IST = timezone(timedelta(hours=5, minutes=30))
BANDS = (2, 5, 10, 20)          # daily price limits in percent
TICK = 0.05                     # price step on both exchanges for most stocks
CACHE_DIR = "cache"
NSE_EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST", "IL", "BL", "SZ"}
FORWARD = 5                     # sessions looked at when measuring outcomes

SIGNAL_LABELS = {
    "locked_down": "Locked at lower circuit",
    "no_trades": "No trades in latest session",
    "streak_broke": "Streak ended today",
    "failed_circuit": "Touched upper circuit, closed lower",
    "vol_spike": "Volume spike",
    "range_wide": "Trading range widened",
    "fell": "Fell 2%+ after the run",
    "delivery": "Delivery share fell (NSE)",
    "drawdown": "10%+ below post-run peak",
    "extended": "Run gained 100%+",
    "thin": "Thin trading",
    "cooling": "Streak ended 2-5 sessions ago",
}


# ---------------------------------------------------------------- download ---
class Missing(Exception):
    """The exchange has no file for that day (holiday or not published yet)."""


class Blocked(Exception):
    """The exchange refused the request or kept timing out."""


class Fetcher:
    def __init__(self, offline_dir=None, pause=0.6):
        self.offline_dir = offline_dir
        self.pause = pause
        self.last = 0.0
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(self, url, referer):
        if self.offline_dir:
            path = os.path.join(self.offline_dir, os.path.basename(url))
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    return fh.read()
            raise Missing(url)

        wait = self.pause - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        }
        last_err = "unknown error"
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers=headers)
                with self.opener.open(req, timeout=45) as resp:
                    data = resp.read()
                self.last = time.time()
                return data
            except urllib.error.HTTPError as e:
                self.last = time.time()
                if e.code in (404, 410):
                    raise Missing(url)
                last_err = f"HTTP {e.code}"
                time.sleep(5 * (attempt + 1) if e.code in (403, 429, 503) else 2)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = str(e)
                time.sleep(3 * (attempt + 1))
        raise Blocked(f"{os.path.basename(url)}: {last_err}")


def nse_urls(d):
    base = "https://nsearchives.nseindia.com"
    return [
        f"{base}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv",
        f"{base}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
    ]


def bse_urls(d):
    base = "https://www.bseindia.com/download/BhavCopy/Equity"
    return [
        f"{base}/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV",
        f"{base}/EQ_ISINCODE_{d:%d%m%y}.zip",
        f"{base}/EQ{d:%d%m%y}_CSV.ZIP",
    ]


REFERER = {
    "NSE": "https://www.nseindia.com/all-reports",
    "BSE": "https://www.bseindia.com/markets/MarketInfo/BhavCopy.aspx",
}


# ------------------------------------------------------------------ parsing ---
def parse_any(raw, exch):
    """Turn any of the supported exchange files into one standard table."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), dtype=str, skipinitialspace=True,
                     encoding_errors="replace")
    df.columns = [str(c).strip().upper() for c in df.columns]
    cols = set(df.columns)

    def num(c):
        if c in cols:
            return pd.to_numeric(df[c].str.strip(), errors="coerce")
        return pd.Series(np.nan, index=df.index)

    def txt(c):
        if c in cols:
            return df[c].fillna("").str.strip()
        return pd.Series("", index=df.index)

    if {"SYMBOL", "PREV_CLOSE", "CLOSE_PRICE"} <= cols:          # NSE full bhavcopy
        out = pd.DataFrame({
            "code": txt("SYMBOL"), "symbol": txt("SYMBOL"), "name": "",
            "series": txt("SERIES"),
            "prev": num("PREV_CLOSE"),
            "high": num("HIGH_PRICE"), "low": num("LOW_PRICE"),
            "close": num("CLOSE_PRICE"), "volume": num("TTL_TRD_QNTY"),
            "deliv": num("DELIV_PER"),
        })
        out = out[out["series"].isin(NSE_EQUITY_SERIES)]
    elif {"TCKRSYMB", "CLSPRIC", "PRVSCLSGPRIC"} <= cols:        # UDiFF layout
        symbol = txt("TCKRSYMB")
        code = txt("FININSTRMID") if exch == "BSE" else symbol
        code = code.where(code != "", symbol)
        out = pd.DataFrame({
            "code": code, "symbol": symbol, "name": txt("FININSTRMNM"),
            "series": txt("SCTYSRS"),
            "prev": num("PRVSCLSGPRIC"),
            "high": num("HGHPRIC"), "low": num("LWPRIC"),
            "close": num("CLSPRIC"), "volume": num("TTLTRADGVOL"),
            "deliv": np.nan,
        })
        if exch == "NSE":
            out = out[out["series"].isin(NSE_EQUITY_SERIES)]
    elif {"SC_CODE", "CLOSE", "PREVCLOSE"} <= cols:              # older BSE layout
        name = txt("SC_NAME")
        out = pd.DataFrame({
            "code": txt("SC_CODE"), "symbol": name, "name": name,
            "series": txt("SC_GROUP"),
            "prev": num("PREVCLOSE"),
            "high": num("HIGH"), "low": num("LOW"),
            "close": num("CLOSE"), "volume": num("NO_OF_SHRS"),
            "deliv": np.nan,
        })
    else:
        raise ValueError(f"unrecognised file layout, columns were: {sorted(cols)[:20]}")

    out = out[out["code"] != ""]
    out.insert(0, "exch", exch)
    return out.reset_index(drop=True)


def fetch_day(fetcher, exch, d):
    urls = nse_urls(d) if exch == "NSE" else bse_urls(d)
    for url in urls:
        try:
            raw = fetcher.get(url, REFERER[exch])
        except Missing:
            continue
        if raw.lstrip()[:1] == b"<":      # an HTML page instead of a data file
            continue
        return parse_any(raw, exch)
    return None


def cache_path(exch, d, ext="csv.gz"):
    return os.path.join(CACHE_DIR, f"{exch}_{d:%Y%m%d}.{ext}")


def load_exchange(fetcher, exch, end_day, days, notes):
    """Return a list of per-day tables (each with a 'date' column), newest first."""
    ds = [end_day - timedelta(n) for n in range(days + 1)]
    ds = [d for d in ds if d.weekday() < 5]
    frames, blocked, fresh = [], False, 0
    for d in ds:
        cp = cache_path(exch, d)
        if os.path.exists(cp):
            df = pd.read_csv(cp, dtype={"code": str, "symbol": str, "name": str, "series": str})
            df[["symbol", "name", "series"]] = df[["symbol", "name", "series"]].fillna("")
            frames.append(df.assign(date=pd.Timestamp(d)))
            continue
        if os.path.exists(cache_path(exch, d, "none")) or blocked:
            continue
        try:
            df = fetch_day(fetcher, exch, d)
        except Blocked as e:
            blocked = True
            notes.append(f"{exch}: the exchange refused or timed out ({e}). "
                         "Showing only the days saved before.")
            continue
        except ValueError as e:
            notes.append(f"{exch} {d:%d %b %Y}: could not read the file ({e}).")
            continue
        if df is None:
            if (end_day - d).days > 3:       # an old gap is a holiday, remember it
                open(cache_path(exch, d, "none"), "w").close()
            continue
        df.to_csv(cp, index=False)
        frames.append(df.assign(date=pd.Timestamp(d)))
        fresh += 1
        if fresh % 25 == 0:
            print(f"  {exch}: downloaded {fresh} new days...", flush=True)
    print(f"{exch}: {len(frames)} sessions loaded ({fresh} newly downloaded)", flush=True)
    return frames


def load_names(fetcher):
    path = os.path.join(CACHE_DIR, "EQUITY_L.csv")
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400
    raw = None
    if not fresh:
        try:
            raw = fetcher.get("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                              "https://www.nseindia.com/")
            with open(path, "wb") as fh:
                fh.write(raw)
        except (Missing, Blocked):
            raw = None
    if raw is None and os.path.exists(path):
        with open(path, "rb") as fh:
            raw = fh.read()
    if raw is None:
        return {}
    try:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, skipinitialspace=True)
        df.columns = [c.strip().upper() for c in df.columns]
        return dict(zip(df["SYMBOL"].str.strip(), df["NAME OF COMPANY"].str.strip()))
    except Exception:
        return {}


# -------------------------------------------------------------------- panel ---
def build_panel(frames):
    df = pd.concat(frames, ignore_index=True)
    df["key"] = df["exch"] + ":" + df["code"].astype(str)
    df = df[(df["close"] > 0) & (df["prev"] > 0)]
    df["_rank"] = df["series"].isin(NSE_EQUITY_SERIES - {"BL", "IL"}).map({True: 0, False: 1})
    df = (df.sort_values(["date", "key", "_rank"])
            .drop_duplicates(["date", "key"], keep="first"))
    dates = sorted(df["date"].unique())
    keys = df["key"].unique()
    arrays = {}
    for field in ("prev", "high", "low", "close", "volume", "deliv"):
        arrays[field] = (df.pivot(index="date", columns="key", values=field)
                           .reindex(index=dates, columns=keys).to_numpy(dtype=float))
    meta = (df.sort_values("date").groupby("key")[["symbol", "name", "series"]].last()
              .reindex(keys))
    return {"dates": [pd.Timestamp(x) for x in dates], "keys": list(keys), "meta": meta, **arrays}


def circuit_flags(P, min_price):
    """Mark every stock-day that closed at its upper or lower price limit."""
    prev, hi, lo, cl, vol = P["prev"], P["high"], P["low"], P["close"], P["volume"]
    with np.errstate(all="ignore"):
        ret = cl / prev - 1
        slack = TICK / prev + 0.0005
        ok = (vol > 0) & (prev >= min_price)
        at_high = cl >= hi - TICK * 1.01
        at_low = cl <= lo + TICK * 1.01
        up = np.zeros(cl.shape, bool)
        dn = np.zeros(cl.shape, bool)
        band_up = np.zeros(cl.shape)
        band_dn = np.zeros(cl.shape)
        for b in BANDS:
            m = b / 100
            hit_up = ok & at_high & (ret >= m - slack) & (ret <= m + 0.0006)
            hit_dn = ok & at_low & (ret <= -m + slack) & (ret >= -m - 0.0006)
            band_up = np.where(hit_up & ~up, b, band_up)
            band_dn = np.where(hit_dn & ~dn, b, band_dn)
            up |= hit_up
            dn |= hit_dn
    return ret, up, dn, band_up, band_dn


def runs_of(flags, min_len):
    """List of (start, end) index pairs where flags stay True for min_len+ days."""
    padded = np.concatenate(([False], flags, [False]))
    d = np.diff(padded.astype(np.int8))
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0] - 1
    return [(int(s), int(e)) for s, e in zip(starts, ends) if e - s + 1 >= min_len]


def pick_run(runs, i, max_age, min_len):
    """The most recent qualifying run as it looked on day i (no peeking ahead)."""
    best = None
    for s, e in runs:
        if s > i:
            break
        ei = min(e, i)
        if ei - s + 1 >= min_len and i - ei <= max_age:
            best = (s, ei)
    return best


# ------------------------------------------------------------------ signals ---
def level_for(score):
    if score >= 7:
        return "High"
    if score >= 4:
        return "Elevated"
    if score >= 2:
        return "Watch"
    return "Low"


def evaluate(A, i, run, exch, day_word="today"):
    """Score the warning signs for one stock on day i."""
    s, e = run
    n_run = e - s + 1
    since_end = i - e
    prev, high, low, close, vol = A["prev"], A["high"], A["low"], A["close"], A["volume"]

    bands = A["band_up"][s:e + 1]
    bands = bands[bands > 0].astype(int)
    band = int(np.bincount(bands).argmax()) if len(bands) else 0

    score, codes, reasons = 0, [], []

    def add(code, pts, text):
        nonlocal score
        score += pts
        codes.append(code)
        reasons.append(text)

    lock_k = 0
    if A["down"][i]:
        j = i
        while j >= 0 and A["down"][j]:
            lock_k += 1
            j -= 1
        add("locked_down", 6, f"Locked at its lower circuit for {lock_k} session"
                              f"{'s' if lock_k > 1 else ''}. Sellers cannot get out.")
    elif since_end == 1:
        add("streak_broke", 4, f"Its {n_run}-session upper-circuit streak ended {day_word}.")
        touched = high[i] >= prev[i] * (1 + band / 100) - (TICK + 0.0005 * prev[i])
        if touched and not A["up"][i]:
            add("failed_circuit", 3, "Touched the upper circuit during the day but closed below it.")
    elif 2 <= since_end <= 5:
        add("cooling", 1, f"The streak ended {since_end} sessions ago.")

    run_vol = np.nanmedian(vol[s:e + 1])
    vol_ratio = float(vol[i] / run_vol) if np.isfinite(run_vol) and run_vol > 0 and np.isfinite(vol[i]) else float("nan")
    if since_end <= 5 and np.isfinite(vol_ratio):
        if vol_ratio >= 6:
            add("vol_spike", 3, f"Volume was {vol_ratio:.0f}x a typical day of the streak.")
        elif vol_ratio >= 3:
            add("vol_spike", 2, f"Volume was {vol_ratio:.0f}x a typical day of the streak.")

    with np.errstate(all="ignore"):
        rng_i = (high[i] - low[i]) / prev[i]
        rng_run = np.nanmedian((high[s:e + 1] - low[s:e + 1]) / prev[s:e + 1])
    if since_end <= 5 and np.isfinite(rng_i) and rng_i >= 0.015 and \
            rng_i >= 3 * max(rng_run if np.isfinite(rng_run) else 0, 0.002):
        add("range_wide", 1, f"The day's trading range widened to {rng_i * 100:.1f}%, "
                             "after near-zero ranges during the streak.")

    ret_i = A["ret"][i]
    if since_end > 0 and not A["down"][i] and np.isfinite(ret_i) and ret_i <= -0.02:
        add("fell", 1, f"Fell {abs(ret_i) * 100:.1f}% {day_word}.")

    deliv_now = A["deliv"][i]
    deliv_run = np.nanmean(A["deliv"][s:e + 1]) if exch == "NSE" else float("nan")
    if since_end <= 3 and np.isfinite(deliv_now) and np.isfinite(deliv_run) and \
            deliv_run > 0 and deliv_now <= 0.6 * deliv_run:
        add("delivery", 1, f"Share of traded shares actually delivered fell to {deliv_now:.0f}% "
                           f"(streak average {deliv_run:.0f}%).")

    drawdown = 0.0
    if since_end > 0:
        peak = np.nanmax(close[e:i + 1])
        if np.isfinite(peak) and np.isfinite(close[i]):
            drawdown = float(close[i] / peak - 1)
            if drawdown <= -0.10:
                add("drawdown", 1, f"{abs(drawdown) * 100:.0f}% below its peak since the streak ended.")

    base = prev[s]
    gain = float(close[e] / base - 1) if base > 0 and np.isfinite(close[e]) else 0.0
    if gain >= 1.0:
        add("extended", 1, f"Gained {gain * 100:.0f}% over the streak.")

    lo_i = max(0, i - 19)
    avg_value = float(np.nanmean(vol[lo_i:i + 1] * close[lo_i:i + 1])) / 1e5   # rupees lakh
    if np.isfinite(avg_value) and avg_value < 25:
        add("thin", 1, f"Thin trading: about Rs {avg_value:.0f} lakh changes hands on a typical day.")

    return {
        "score": score, "level": level_for(score), "codes": codes, "reasons": reasons,
        "n_run": n_run, "since_end": since_end, "band": band, "gain": gain,
        "vol_ratio": vol_ratio, "avg_value": avg_value, "drawdown": drawdown,
        "lock_k": lock_k, "run": (s, e),
    }


def status_text(ev, i):
    if ev["lock_k"]:
        return f"Locked at lower circuit, {ev['lock_k']} session{'s' if ev['lock_k'] > 1 else ''}"
    if ev["since_end"] == 0:
        return f"At upper circuit, session {ev['n_run']} of the streak"
    if ev["since_end"] == 1:
        return f"Streak of {ev['n_run']} sessions ended today"
    return f"Streak of {ev['n_run']} sessions ended {ev['since_end']} sessions ago"


# --------------------------------------------------------------- per exchange ---
def analyse_exchange(exch, P, args, names):
    n_dates, n_keys = P["close"].shape
    ret, up, dn, band_up, band_dn = circuit_flags(P, args.min_price)

    streak = np.zeros(up.shape, dtype=np.int32)
    for t in range(n_dates):
        streak[t] = (streak[t - 1] + 1) * up[t] if t else up[t]
    candidates = np.where(streak.max(axis=0) >= args.min_streak)[0]

    info = {
        "sessions": n_dates,
        "last_date": P["dates"][-1].strftime("%Y-%m-%d"),
        "locked_up_now": int(up[-1].sum()),
        "locked_down_now": int(dn[-1].sum()),
        "candidates": int(len(candidates)),
    }

    stocks, events = [], []
    last = n_dates - 1
    for c in candidates:
        A = {k: P[k][:, c] for k in ("prev", "high", "low", "close", "volume", "deliv")}
        A.update(ret=ret[:, c], up=up[:, c], down=dn[:, c],
                 band_up=band_up[:, c], band_dn=band_dn[:, c])
        runs = runs_of(A["up"], args.min_streak)
        if not runs:
            continue

        # --- history replay: how did each past warning turn out? ---
        seen = set()
        for s, e in runs:
            for i in range(s + args.min_streak - 1, min(e + args.max_age, last - FORWARD) + 1):
                if i in seen or not np.isfinite(A["close"][i]):
                    continue
                seen.add(i)
                run = pick_run(runs, i, args.max_age, args.min_streak)
                if run is None:
                    continue
                ev = evaluate(A, i, run, exch)
                fwd = A["close"][i + 1:i + 1 + FORWARD] / A["close"][i] - 1
                if not np.isfinite(fwd).any():
                    continue
                last_valid = fwd[np.isfinite(fwd)][-1]
                events.append({
                    "level": ev["level"], "codes": ev["codes"],
                    "ret": float(last_valid),
                    "drop": bool(np.nanmin(fwd) <= -0.05),
                    "lock": bool(A["down"][i + 1:i + 1 + FORWARD].any()),
                })

        # --- today's reading ---
        i = last
        ghost = False
        if not np.isfinite(A["close"][i]):
            valid = np.where(np.isfinite(A["close"][:i]))[0]
            if len(valid) == 0 or i - valid[-1] > 3:
                continue
            i = int(valid[-1])
            ghost = True
        run = pick_run(runs, i, args.max_age, args.min_streak)
        if run is None:
            continue
        ev = evaluate(A, i, run, exch, "on its last traded day" if ghost else "today")
        status = status_text(ev, i)
        if ghost:
            ev["score"] += 3
            ev["codes"].append("no_trades")
            ev["reasons"].insert(0, "No trades in the latest session. It may be locked with no one "
                                    "willing to trade, or suspended.")
            ev["level"] = level_for(ev["score"])
            status = "No trades in the latest session"
        s, e = ev["run"]
        lo_c = max(0, last - 44)
        chart = []
        for j in range(lo_c, n_dates):
            v = A["close"][j]
            f = 1 if A["up"][j] else (-1 if A["down"][j] else 0)
            chart.append([None if not np.isfinite(v) else round(float(v), 2), f])
        symbol = P["meta"].iloc[c]["symbol"]
        name = P["meta"].iloc[c]["name"] or names.get(symbol, "")
        stocks.append({
            "id": P["keys"][c], "exch": exch, "symbol": symbol or P["keys"][c].split(":")[1],
            "name": name, "series": P["meta"].iloc[c]["series"],
            "price": round(float(A["close"][i]), 2),
            "band": ev["band"], "status": status, "level": ev["level"], "score": ev["score"],
            "codes": ev["codes"], "reasons": ev["reasons"],
            "run_len": ev["n_run"], "since_end": ev["since_end"] + (last - i),
            "running": ev["since_end"] == 0 and not ghost,
            "run_start": P["dates"][s].strftime("%Y-%m-%d"),
            "run_end": P["dates"][e].strftime("%Y-%m-%d"),
            "run_gain_pct": round(ev["gain"] * 100, 1),
            "vol_ratio": None if not np.isfinite(ev["vol_ratio"]) else round(ev["vol_ratio"], 1),
            "avg_value_lakh": None if not np.isfinite(ev["avg_value"]) else round(ev["avg_value"], 1),
            "drawdown_pct": round(ev["drawdown"] * 100, 1),
            "chart": chart,
        })
    return stocks, events, info


def summarise(rows):
    if not rows:
        return None
    n = len(rows)
    return {
        "n": n,
        "avg_ret": round(100 * sum(r["ret"] for r in rows) / n, 1),
        "drop": round(100 * sum(r["drop"] for r in rows) / n, 1),
        "lock": round(100 * sum(r["lock"] for r in rows) / n, 1),
    }


def build_stats(events):
    if len(events) < 30:
        return None
    by_level = {lv: summarise([r for r in events if r["level"] == lv])
                for lv in ("Low", "Watch", "Elevated", "High")}
    by_signal = []
    for code, label in SIGNAL_LABELS.items():
        if code in ("locked_down", "no_trades"):
            continue
        rows = [r for r in events if code in r["codes"]]
        s = summarise(rows)
        if s:
            by_signal.append({"code": code, "label": label, **s})
    by_signal.sort(key=lambda x: -x["drop"])
    return {"events": len(events), "forward": FORWARD, "levels": by_level, "signals": by_signal}


# --------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join("site", "data.json"))
    ap.add_argument("--days", type=int, default=400, help="calendar days of history (default 400)")
    ap.add_argument("--min-streak", type=int, default=5, help="consecutive upper-circuit days that count as a run")
    ap.add_argument("--max-age", type=int, default=15, help="keep stocks whose run ended this many sessions ago or less")
    ap.add_argument("--min-price", type=float, default=10.0, help="ignore stocks priced below this (rupees)")
    ap.add_argument("--pause", type=float, default=0.6, help="seconds between downloads")
    ap.add_argument("--end-date", help="treat this date (YYYY-MM-DD) as today")
    ap.add_argument("--offline-dir", help="read exchange files from this folder instead of downloading")
    ap.add_argument("--cache", default="cache")
    args = ap.parse_args()

    global CACHE_DIR
    CACHE_DIR = args.cache
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    end_day = (datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date
               else datetime.now(IST).date())
    fetcher = Fetcher(args.offline_dir, args.pause)
    notes = []
    names = load_names(fetcher)

    all_stocks, all_events, infos = [], [], {}
    for exch in ("NSE", "BSE"):
        frames = load_exchange(fetcher, exch, end_day, args.days, notes)
        if len(frames) < 8:
            notes.append(f"{exch}: only {len(frames)} sessions of data were available, so {exch} is not "
                         "being scanned. See the README for what to try.")
            continue
        P = build_panel(frames)
        stocks, events, info = analyse_exchange(exch, P, args, names)
        all_stocks += stocks
        all_events += events
        infos[exch] = info
        print(f"{exch}: {len(stocks)} stocks in or just out of a run, through {info['last_date']}", flush=True)

    if not infos:
        print("No exchange data could be loaded. Nothing written.", file=sys.stderr)
        for n in notes:
            print(" -", n, file=sys.stderr)
        sys.exit(1)

    # Dual-listed stocks: keep the listing with the stronger reading, NSE on ties.
    best = {}
    for s in all_stocks:
        k = s["symbol"].upper()
        cur = best.get(k)
        if cur is None or s["score"] > cur["score"] or (s["score"] == cur["score"] and s["exch"] == "NSE"):
            if cur is not None:
                s["also_on"] = cur["exch"]
            best[k] = s
        else:
            cur["also_on"] = s["exch"]
    stocks = sorted(best.values(), key=lambda s: (-s["score"], -s["run_len"], s["symbol"]))[:500]

    counts = {lv: sum(1 for s in stocks if s["level"] == lv) for lv in ("High", "Elevated", "Watch", "Low")}
    counts["total"] = len(stocks)
    data = {
        "generated_at": datetime.now(IST).isoformat(timespec="minutes"),
        "data_dates": {k: v["last_date"] for k, v in infos.items()},
        "sessions": {k: v["sessions"] for k, v in infos.items()},
        "market": {k: {"locked_up": v["locked_up_now"], "locked_down": v["locked_down_now"]}
                   for k, v in infos.items()},
        "params": {"min_streak": args.min_streak, "max_age": args.max_age, "min_price": args.min_price},
        "notes": notes,
        "counts": counts,
        "stocks": stocks,
        "stats": build_stats(all_events),
        "signal_labels": SIGNAL_LABELS,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {args.out}: {counts}", flush=True)


if __name__ == "__main__":
    main()
