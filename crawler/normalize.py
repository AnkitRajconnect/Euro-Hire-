"""Turn adapter output into one consistent job record, and decide if it is in Europe."""
from __future__ import annotations

import re

from . import geo

SNIPPET_LEN = 500  # we link to the employer for the full text; we don't repost it

REMOTE_RE = re.compile(
    r"\bremote\b|home ?office|work from home|télétravail|teletravail|thuiswerk|telelavoro|teletrabajo|fernarbeit|distributed", re.I)
EUROPE_REMOTE_RE = re.compile(r"\b(europe|european|emea|eu|eea|cet|cest|eet)\b", re.I)
NON_EUROPE_RE = re.compile(
    r"\b(united states|usa|u\.s\.|canada|india|brazil|mexico|australia|singapore|japan|"
    r"north america|latam|apac|new york|san francisco|toronto|bangalore|bengaluru|sydney)\b", re.I)

US_RE = re.compile(r"\bUS\b|\bU\.S\.")  # case-sensitive on purpose ("join us")

ROLE_RULES = [
    ("data", r"data (scientist|engineer|analyst)|machine learning|\bml\b|\bai\b|research scientist|analytics|\bbi\b|"
             r"analyste de données|analyste data|données|data-analist|datenanalyst|datenwissenschaftler"),
    ("engineering", r"engineer|developer|software|devops|sre\b|backend|frontend|front-end|back-end|full.?stack|"
                    r"security|platform|architect|qa\b|mobile|ios|android|embedded|technical lead|ingénieur|ingenieur|"
                    r"développeur|developpeur|entwickler|ontwikkelaar|softwareentwickler"),
    ("product", r"product (manager|owner|lead|director)|\bpm\b|head of product|chef de produit|productmanager|produktmanager"),
    ("design", r"design|ux|ui\b|creative|brand|graphiste|grafikdesigner|vormgever"),
    ("sales", r"sales|account (executive|manager)|business development|\bbdr\b|\bsdr\b|partnership|commercial manager|"
               r"commerciale?|accountmanager|vertrieb|sales manager|responsable commercial|sales consultant"),
    ("marketing", r"marketing|growth|content|seo|communications|\bpr\b|social media|public relations|marketeer|"
                   r"responsable marketing|kommunikationsmanager"),
    ("support", r"customer (success|support|service)|support (agent|specialist|engineer)|onboarding|client success|"
                r"service client|kundendienst|klantenservice|customer experience"),
    ("finance", r"finance|accountant|accounting|controller|tax|audit|risk|treasury|financial analyst|comptable|buchhalter|boekhouder|"
                 r"payroll specialist|credit analyst|actuary|actuaire"),
    ("legal", r"legal|lawyer|solicitor|barrister|paralegal|counsel|compliance|privacy officer|data protection|juriste|jurist|rechtsanwalt"),
    ("people", r"recruit|talent|people|\bhr\b|human resources|learning and development|payroll|recruteur|ressources humaines|"
                r"personalwesen|personalsachbearbeiter|recruiter"),
    ("procurement", r"procurement|purchasing|strategic sourcing|category buyer|buyer|einkauf|inkäufer|inkaeufer|inkoop"),
    ("admin", r"administrative|administrator|office manager|executive assistant|personal assistant|office coordinator|"
               r"assistant administratif|administratief medewerker|büroassistent|bueroassistent|sachbearbeiter"),
    ("business", r"business analyst|management consultant|strategy consultant|business consultant|strategy manager|"
                 r"business operations analyst|commercial analyst|berater|consultant|conseil en stratégie|bedrijfsadviseur|"
                 r"managing director|general manager|store manager|retail manager|hotel manager|branch manager"),
    ("operations", r"operations|supply chain|logistics (analyst|manager|coordinator)|project manager|program manager|"
                   r"business operations|process improvement|service delivery|chef de projet|projectleider|projektleiter|"
                   r"supply.chain.manager|logistikmanager|warehouse manager|magazijnmanager|lagerleiter"),
    ("healthcare", r"physician|medical doctor|doctor|nurse|nursing|pharmacist|pharmacy manager|dentist|dentistry|"
                   r"clinical researcher|clinical operations|physiotherapist|psychologist|radiologist|midwife|"
                   r"médecin|infirmier|infirmière|pharmacien|pharmacienne|arts|verpleegkundige|apotheker|arzt|ärztin|krankenschwester"),
    ("education", r"teacher|lecturer|professor|academic|education manager|school administrator|learning designer|"
                  r"enseignant|enseignante|professeur|docent|leerkracht|lehrer|dozent"),
    ("leadership", r"\bceo\b|\bcfo\b|\bcoo\b|\bcto\b|chief executive|chief financial|chief operating|"
                   r"chief technology|executive director|directeur général|directrice générale|geschäftsführer|algemeen directeur"),
]

ROLE_RES = [(name, re.compile(rx, re.I)) for name, rx in ROLE_RULES]

LEVEL_RULES = [
    ("intern", r"\bintern(ship)?\b|\bpraktik|\bstage\b|stagiaire|werkstudent|working student|apprentice|alternance"),
    ("junior", r"\bjunior\b|\bgraduate\b|entry.?level|\bassociate\b|trainee|\bjr\b"),
    ("lead", r"\b(lead|staff|principal|head of|director|vp|chief)\b|\bmanager\b"),
    ("senior", r"\bsenior\b|\bsr\b|\bexperienced\b"),
]
LEVEL_RES = [(name, re.compile(rx, re.I)) for name, rx in LEVEL_RULES]

STOP = {
    "en": {"the", "and", "you", "with", "for", "our", "will", "are", "your", "team", "have", "work"},
    "de": {"und", "die", "der", "das", "wir", "für", "mit", "ein", "eine", "sie", "bei", "von"},
    "fr": {"les", "des", "pour", "nous", "vous", "une", "est", "dans", "avec", "sur", "notre"},
    "nl": {"het", "een", "voor", "wij", "jij", "met", "van", "ons", "bij", "naar", "ben"},
    "es": {"los", "las", "para", "con", "una", "nuestro", "como", "del", "por", "equipo"},
    "it": {"gli", "per", "con", "una", "nostro", "sono", "della", "del", "che", "team"},
}

SAL_RE = re.compile(
    r"(?P<cur>€|EUR|£|GBP|CHF|SEK|NOK|DKK|PLN|CZK)\s?"
    r"(?P<lo>\d{1,3}(?:[.,\s]\d{3})+|\d{2,3}\s?[kK])\s?"
    r"(?:-|–|—|to|bis|à)\s?(?:(?:€|EUR|£|GBP|CHF)\s?)?"
    r"(?P<hi>\d{1,3}(?:[.,\s]\d{3})+|\d{2,3}\s?[kK])"
    r"|(?P<lo2>\d{1,3}(?:[.,]\d{3})+|\d{2,3}\s?[kK])\s?(?:-|–|—|to|bis)\s?"
    r"(?P<hi2>\d{1,3}(?:[.,]\d{3})+|\d{2,3}\s?[kK])\s?(?P<cur2>€|EUR|£|GBP|CHF|SEK|NOK|DKK|PLN)"
)


def _num(s: str) -> int:
    s = s.strip().lower().replace(" ", "")
    if s.endswith("k"):
        return int(s[:-1]) * 1000
    return int(re.sub(r"[.,]", "", s))


def find_salary(text: str):
    """Pull an annual salary range out of free text if the employer published one."""
    m = SAL_RE.search(text or "")
    if not m:
        return None
    cur = m.group("cur") or m.group("cur2")
    lo = _num(m.group("lo") or m.group("lo2"))
    hi = _num(m.group("hi") or m.group("hi2"))
    cur = {"€": "EUR", "£": "GBP"}.get(cur, cur)
    if lo > hi:
        lo, hi = hi, lo
    if hi < 8000 or hi > 1_000_000:  # probably hourly/monthly or noise
        return None
    return {"min": lo, "max": hi, "currency": cur}


def detect_language(text: str) -> str:
    words = re.findall(r"[a-zà-ÿ]+", (text or "").lower())
    if len(words) < 8:
        return "en"
    scores = {lang: sum(w in stops for w in words) for lang, stops in STOP.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 3 else "en"


def classify(title: str, department: str | None):
    role = "other"
    hay = title or ""
    for name, rx in ROLE_RES:
        if rx.search(hay):
            role = name
            break
    else:
        for name, rx in ROLE_RES:
            if department and rx.search(department):
                role = name
                break
    level = "mid"
    for name, rx in LEVEL_RES:
        if rx.search(title or ""):
            level = name
            break
    return role, level


def normalize(raw: dict, company: dict, now_iso: str) -> dict | None:
    """Return a final job dict, or None if the job is not in/for Europe."""
    title = (raw.get("title") or "").strip()
    url = (raw.get("url") or "").strip()
    if not title or not url.startswith("http"):
        return None

    loc_text = (raw.get("location") or "").strip()
    desc = raw.get("description") or ""
    hint = (raw.get("country") or "").upper() or None
    if not hint and raw.get("country_text"):
        for rx, cc in geo._COUNTRY_INDEX:
            if rx.search(geo._norm(raw["country_text"])) or raw["country_text"].upper() == cc:
                hint = cc
                break

    blob = f"{loc_text} {title}"
    remote = bool(raw.get("remote")) or bool(REMOTE_RE.search(blob))
    place = geo.locate(loc_text, hint)
    if place is None and hint is None:
        # Some boards put the city only in the title ("Engineer (Berlin)")
        place = geo.locate(title)

    in_europe = place is not None and place["country"] in geo.EUROPE
    if not in_europe:
        if not remote:
            return None
        europe_signal = bool(EUROPE_REMOTE_RE.search(blob))
        non_europe_signal = bool(NON_EUROPE_RE.search(blob) or US_RE.search(blob))
        if non_europe_signal and not europe_signal:
            return None
        generic = not loc_text or loc_text.lower().strip(" .") in ("remote", "anywhere", "worldwide", "global")
        europe_first = company.get("europe_first", company.get("hq") in geo.EUROPE)
        if not (europe_signal or (generic and europe_first)):
            return None
        place = None

    role, level = classify(title, raw.get("department"))
    # Unknown occupations are held back for review instead of being published
    # under a broad "Other" category. Manual/service occupations are screened
    # before normalisation in company_security.screen_vacancy().
    if role == "other":
        return None
    salary = find_salary(desc[:6000] + " " + loc_text)
    language = detect_language(desc[:1500] or title)

    cc = company.get("company_key") or company["slug"]
    verification = company.get("verification") or {}
    return {
        "id": f"{company['ats']}:{cc}:{raw['id']}",
        "company": company["name"],
        "company_slug": company.get("slug") or cc,
        "company_registry_id": (verification.get("registry") or {}).get("id"),
        "company_registry_country": (verification.get("registry") or {}).get("country"),
        "company_registry_verified": bool((verification.get("registry") or {}).get("verified")),
        "company_registry_checked_at": (verification.get("registry") or {}).get("checked_at"),
        "career_source_verified": bool((verification.get("source") or {}).get("verified")),
        "source_last_checked": now_iso,
        "title": title,
        "url": url,
        "location": loc_text or ("Remote" if remote else ""),
        "city": place["city"] if place else None,
        "country": place["country"] if place else None,
        "lat": place["lat"] if place else None,
        "lon": place["lon"] if place else None,
        "remote": remote,
        "role": role,
        "level": level,
        "language": language,
        "salary": salary,
        "department": raw.get("department"),
        "posted": raw.get("posted"),
        "snippet": desc[:SNIPPET_LEN].rsplit(" ", 1)[0] if len(desc) > SNIPPET_LEN else desc,
        "first_seen": now_iso,
        "last_seen": now_iso,
        "ats": company["ats"],
    }
