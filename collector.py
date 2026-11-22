"""Collecteur public incremental OIT + UNGM (zero donnee privee, stdlib uniquement)."""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
PUBLIC_PATH = ROOT / "feed_public.json"
DELTA_PATH = ROOT / "feed_delta.json"

ILO_LIST_URL = "https://webapps.ilo.org/webcommon/php/ungm-rss.php"
UNGM_NOTICE_URL = "https://www.ungm.org/Public/Notice/{notice_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
}

EXCLUDE_PATTERNS = re.compile(
    r"\b("
    r"air\s+tickets?|flight\s+tickets?|travel\s+agency|dispensadores?\s+de\s+agua|"
    r"water\s+dispensers?|spare\s+parts|vehicles?|automobiles?|trucks?|ambulances?|"
    r"motorcycles?|fuel\s+supply|diesel|petrol|catering|hotel\s+accommodation|"
    r"hotel\s+services?|hotel\s+and\s+conference|s[eé]lection\s+d['’]un\s+h[oô]tel|"
    r"meeting\s+venue|conference\s+venue|venue\s+and\s+catering|security\s+guard|"
    r"cleaning\s+services?|cleaning\s+materials?|janitorial|servicio\s+de\s+limpieza|"
    r"cable\s+tv|televisi[oó]n\s+por\s+cable|telefon[ií]a\s+fija|service\s+t[eé]l[eé]phonique|"
    r"furniture|office\s+supplies|stationer(?:y|ies)|stationaries|toner|cartridges?|"
    r"insurance\s+broker|corredora\s+de\s+seguros|printing\s+and\s+binding|uniforms?|"
    r"solar\s+panels?\s+supply|construction\s+works|civil\s+works|renovation\s+works|"
    r"logistique\s+d['’]ateliers|servicio\s+de\s+alimentaci[oó]n|achat\s+des?\s+mat[eé]riel"
    r")\b",
    re.IGNORECASE,
)

PROFILE_RELEVANT = re.compile(
    r"collaborat|consult|excoll|train|form|digital|data|IA|\bAI\b|web|software|cyber|"
    r"expert|develop|communication|platform|moodle|python|power\s*bi|study|evaluat",
    re.IGNORECASE,
)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


class _IloListParser(HTMLParser):
    """Parse les elements <li> de webapps.ilo.org/webcommon/php/ungm-rss.php."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str]] = []
        self._in_li = False
        self._in_a = False
        self._in_p = False
        self._href = ""
        self._title_parts: list[str] = []
        self._p_parts: list[str] = []
        self._deadline = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = dict(attrs)
        if tag == "li":
            self._in_li = True
            self._href = ""
            self._title_parts = []
            self._deadline = ""
        elif self._in_li and tag == "a":
            self._in_a = True
            self._href = (attr_map.get("href") or "").strip()
        elif self._in_li and tag == "p":
            self._in_p = True
            self._p_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._in_a:
            self._in_a = False
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join("".join(self._p_parts).split())
            if text.lower().startswith("deadline:"):
                self._deadline = text.split(":", 1)[1].strip()
        elif tag == "li" and self._in_li:
            self._in_li = False
            title = " ".join("".join(self._title_parts).split())
            if self._href and title:
                self.items.append({"url": self._href, "title": title, "deadline_raw": self._deadline})

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._title_parts.append(data)
        elif self._in_p:
            self._p_parts.append(data)


def _fetch_text(url: str, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="ignore")


def _parse_date_iso(raw: str) -> str:
    m = re.search(r"(\d{1,2})[-/\s]+([A-Za-z]+|\d{1,2})[-/\s]+(\d{4})", raw or "")
    if not m:
        return ""
    day, mon_s, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = int(mon_s) if mon_s.isdigit() else MONTHS.get(mon_s, 0)
    if not month:
        return ""
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _extract_ungm_details(notice_id: str) -> dict[str, str]:
    url = UNGM_NOTICE_URL.format(notice_id=notice_id)
    try:
        _, html = _fetch_text(url, timeout=20)
    except Exception:
        return {}
    clean = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = " ".join(re.sub(r"<[^>]+>", " ", clean).split())

    def _find(pattern: str) -> str:
        m = re.search(pattern, text, re.I)
        return m.group(1).strip() if m else ""

    ref = _find(r"\bReference\b\s*[:#]?\s*([A-Za-z0-9_./-]+)")
    if not ref:
        m_rfx = re.search(r"\b(rfx_\d+_[A-Z]+)\b", text)
        ref = m_rfx.group(1) if m_rfx else ""
    pub = _parse_date_iso(_find(r"\bPublished on\b\s*[:#]?\s*([0-9A-Za-z-\s]+?\d{4})"))
    country = _find(r"\bBeneficiary country(?:\(ies\))?\b\s*[:#]?\s*([A-Za-z\s,.'()-]{2,60}?)(?=\s+(?:Deadline|Reference|Published|UNSPSC|Description|Documents|Notice type)\b|$)")
    notice_type = _find(r"\bNotice type\b\s*[:#]?\s*([A-Za-z\s/-]{3,50}?)(?=\s+(?:Deadline|Reference|Published|Beneficiary|UNSPSC)\b|$)")
    is_excoll = bool(re.search(r"external collaborator|individual consultant|excoll|consultant individuel", text, re.I))
    jaggaer = bool(re.search(r"jaggaer|e-?sourcing|iloat|express interest", text, re.I))
    return {
        "ref": ref,
        "pub": pub,
        "pays": country[:50],
        "type_avis": notice_type[:50],
        "ind": "1" if is_excoll else ("?" if not notice_type else "0"),
        "flash": "1" if jaggaer else "0",
    }


def _slug_org(org: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", org.lower()).strip("-")


def _make_fp(org: str, ref_or_id: str, deadline_iso: str) -> str:
    return f"{_slug_org(org)}|{ref_or_id.strip()}|{deadline_iso or 'nodate'}"


def _content_hash(title: str, deadline_iso: str, pays: str, ref: str) -> str:
    payload = f"{title.strip()}|{deadline_iso}|{pays}|{ref}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> None:
    """Execute un cycle incremental de collecte OIT + UNGM."""
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_iso = now.strftime("%Y-%m-%d")

    state = _load_json(STATE_PATH, {"seq": 0, "last_run_utc": "", "known": {}, "sources": {}})
    delta_doc = _load_json(DELTA_PATH, {"maj": "", "sante": {}, "events": []})
    known: dict[str, dict] = state.get("known", {})

    sante: dict[str, str] = {}
    new_events: list[dict] = []
    fetched_items: list[dict[str, str]] = []

    try:
        status, html = _fetch_text(ILO_LIST_URL)
        parser = _IloListParser()
        parser.feed(html)
        fetched_items = parser.items
        sante["oit_ungm_rss"] = f"OK ({status}, {len(fetched_items)} avis lus)"
    except Exception as exc:
        sante["oit_ungm_rss"] = f"ERREUR ({type(exc).__name__}: {exc})"

    enriched_this_run = 0
    max_enrich_per_run = 25
    seq = int(state.get("seq", 0))

    # Prioriser l'enrichissement UNGM sur les avis qui matchent le profil en premier
    fetched_items.sort(key=lambda x: (0 if PROFILE_RELEVANT.search(x["title"]) else 1, x["deadline_raw"]))

    for item in fetched_items:
        url = item["url"]
        m_id = re.search(r"/notice/(\d+)", url, re.I)
        if not m_id:
            continue
        ungm_id = m_id.group(1)
        title = item["title"]
        if EXCLUDE_PATTERNS.search(title):
            continue

        dl_iso = _parse_date_iso(item["deadline_raw"])
        if dl_iso and dl_iso < today_iso:
            continue

        prev = known.get(ungm_id)
        needs_enrich = prev is None or (prev.get("dl") != dl_iso)
        details: dict[str, str] = {}
        if needs_enrich and enriched_this_run < max_enrich_per_run:
            details = _extract_ungm_details(ungm_id)
            enriched_this_run += 1
            time.sleep(0.25)

        ref = details.get("ref") or (prev.get("ref") if prev else "") or f"UNGM-{ungm_id}"
        pays = details.get("pays") or (prev.get("pays") if prev else "")
        pub = details.get("pub") or (prev.get("pub") if prev else "")
        ind = details.get("ind") or (prev.get("ind") if prev else "?")
        flash = details.get("flash") or (prev.get("flash") if prev else "0")

        fp = _make_fp("oit-ilo", f"UNGM-{ungm_id}", dl_iso)
        h = _content_hash(title, dl_iso, pays, ref)

        entry = {
            "fp": fp,
            "src": "oit_ungm",
            "ungm": ungm_id,
            "ref": ref,
            "t": title,
            "org": "OIT / ILO",
            "pays": pays,
            "dl": dl_iso,
            "url": f"https://www.ungm.org/Public/Notice/{ungm_id}",
            "ind": ind,
            "flash": flash,
            "pub": pub,
            "h": h,
            "last_seen": now_iso,
        }

        if prev is None:
            seq += 1
            entry["first_seen"] = now_iso
            known[ungm_id] = entry
            ev = dict(entry)
            ev["seq"] = seq
            ev["ev"] = "NEW"
            ev["cycle_utc"] = now_iso
            new_events.append(ev)
        elif prev.get("h") != h:
            seq += 1
            entry["first_seen"] = prev.get("first_seen", now_iso)
            known[ungm_id] = entry
            ev = dict(entry)
            ev["seq"] = seq
            ev["ev"] = "UPDATED"
            ev["cycle_utc"] = now_iso
            new_events.append(ev)
        else:
            prev["last_seen"] = now_iso

    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    relevant_new = [e for e in new_events if PROFILE_RELEVANT.search(e["t"]) or e.get("ind") == "1"]
    merged_events = [e for e in delta_doc.get("events", []) if e.get("cycle_utc", "") >= cutoff]
    merged_events.extend(relevant_new)
    if len(merged_events) > 60:
        merged_events = merged_events[-60:]

    active_public = [
        v for v in known.values() if not v.get("dl") or v["dl"] >= today_iso
    ]
    active_public.sort(key=lambda x: (x.get("dl") or "9999-99-99", x.get("ungm", "")))

    state["seq"] = seq
    state["last_run_utc"] = now_iso
    state["sources"] = sante
    state["known"] = known

    delta_out = {
        "maj": now_iso,
        "seq_courant": seq,
        "sante": sante,
        "nouveaux_ou_maj_ce_cycle": len(relevant_new),
        "events": merged_events,
    }
    public_out = {
        "maj": now_iso,
        "sante": sante,
        "total_actifs": len(active_public),
        "items": active_public,
    }

    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    delta_lines = [json.dumps({k: delta_out[k] for k in ("maj", "seq_courant", "sante", "nouveaux_ou_maj_ce_cycle")}, ensure_ascii=False, separators=(",", ":"))]
    # Ecriture compacte JSON valide (<= ~65 lignes pour economie maximale de jetons Claude)
    compact_events = ",\n".join(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) for ev in merged_events)
    delta_txt = (
        f'{{"maj":{json.dumps(now_iso)},"seq_courant":{seq},"sante":{json.dumps(sante, ensure_ascii=False, separators=(",",":"))},'
        f'"nouveaux_ou_maj_ce_cycle":{len(relevant_new)},"events":[\n{compact_events}\n]}}\n'
    )
    DELTA_PATH.write_text(delta_txt, encoding="utf-8")
    PUBLIC_PATH.write_text(json.dumps(public_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"COLLECT OK: lus={len(fetched_items)}, actifs={len(active_public)}, enrichis={enriched_this_run}, events_pertinents={len(relevant_new)}, seq={seq}")


if __name__ == "__main__":
    main()
