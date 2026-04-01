"""
Canonical role and seniority normalization.

Roles are mapped to a fixed taxonomy (like domains) so analytics,
filters, and salary comparisons work consistently.
"""
from __future__ import annotations

import re

# ── Seniority ────────────────────────────────────────────────────────────────

SENIORITY_CANONICAL = [
    "trainee",
    "junior",
    "middle",
    "senior",
    "lead",
    "head",
    "c-level",
]

_SENIORITY_MAP: dict[str, str] = {
    "trainee": "trainee",
    "intern": "trainee",
    "internship": "trainee",
    "стажёр": "trainee",
    "стажер": "trainee",
    "junior": "junior",
    "junior+": "junior",
    "jun": "junior",
    "джуниор": "junior",
    "middle": "middle",
    "mid": "middle",
    "mid-level": "middle",
    "intermediate": "middle",
    "миддл": "middle",
    "regular": "middle",
    "senior": "senior",
    "sr": "senior",
    "experienced": "senior",
    "expert": "senior",
    "сеньор": "senior",
    "синьор": "senior",
    "principal": "senior",
    "staff": "senior",
    "lead": "lead",
    "team lead": "lead",
    "teamlead": "lead",
    "tech lead": "lead",
    "тимлид": "lead",
    "лид": "lead",
    "head": "head",
    "director": "head",
    "vp": "head",
    "vice president": "head",
    "руководитель": "head",
    "managing director": "head",
    "c-level": "c-level",
    "cto": "c-level",
    "cpo": "c-level",
    "ceo": "c-level",
    "coo": "c-level",
    "cfo": "c-level",
    "cmo": "c-level",
    "ciso": "c-level",
    "chief": "c-level",
    "executive": "c-level",
    "co-founder": "c-level",
    "founder": "c-level",
    "manager": "middle",
}

_SENIORITY_PREFIX_RE = re.compile(
    r"^(trainee|intern|junior\+?|middle|mid(?:-level)?|senior|sr\.?|lead|head|principal|staff|expert)\b",
    re.I,
)

_COMPOUND_SENIORITY_RE = re.compile(
    r"(junior|middle|mid|senior|lead|head)\s*[/+\-–]\s*(junior|middle|mid|senior|lead|head)",
    re.I,
)

_SENIORITY_RANK = {
    "trainee": 0, "junior": 1, "middle": 2, "senior": 3,
    "lead": 4, "head": 5, "c-level": 6,
}


def normalize_seniority(raw: str | None) -> str | None:
    """Normalize a seniority string to one of the canonical values."""
    if not raw:
        return None
    s = raw.strip().lower()
    if not s:
        return None

    if s in _SENIORITY_MAP:
        return _SENIORITY_MAP[s]

    cm = _COMPOUND_SENIORITY_RE.search(s)
    if cm:
        a = _SENIORITY_MAP.get(cm.group(1).lower(), cm.group(1).lower())
        b = _SENIORITY_MAP.get(cm.group(2).lower(), cm.group(2).lower())
        ra = _SENIORITY_RANK.get(a, 0)
        rb = _SENIORITY_RANK.get(b, 0)
        return a if ra >= rb else b

    for key, val in _SENIORITY_MAP.items():
        if key in s:
            return val

    return s if s in SENIORITY_CANONICAL else None


# ── Roles ────────────────────────────────────────────────────────────────────

ROLE_CANONICAL = [
    "Backend Developer",
    "Frontend Developer",
    "Full Stack Developer",
    "Mobile Developer",
    "Blockchain Developer",
    "Smart Contract Developer",
    "DevOps Engineer",
    "QA Engineer",
    "Security Engineer",
    "System Administrator",
    "Data Analyst",
    "Data Engineer",
    "Data Scientist",
    "ML Engineer",
    "Product Manager",
    "Project Manager",
    "Product Owner",
    "Business Analyst",
    "System Analyst",
    "UI/UX Designer",
    "Graphic Designer",
    "Motion Designer",
    "3D Artist",
    "Game Designer",
    "Marketing Manager",
    "Media Buyer",
    "SEO Specialist",
    "SMM Manager",
    "Content Manager",
    "Community Manager",
    "Traffic Manager",
    "Affiliate Manager",
    "Growth Manager",
    "Performance Marketing Manager",
    "CRM Manager",
    "PR Manager",
    "Sales Manager",
    "Business Development Manager",
    "Account Manager",
    "Partnerships Manager",
    "Financial Manager",
    "Risk Analyst",
    "Compliance Manager",
    "Legal Counsel",
    "HR Manager",
    "Recruiter",
    "Customer Support",
    "Operations Manager",
    "Executive",
    "Other",
]

_ROLE_CANONICAL_LOWER = {r.lower(): r for r in ROLE_CANONICAL}

_ROLE_ALIAS: dict[str, str] = {
    "backend engineer": "Backend Developer",
    "backend rust engineer": "Backend Developer",
    "backend dev": "Backend Developer",
    "php engineer": "Backend Developer",
    "python developer": "Backend Developer",
    ".net developer": "Backend Developer",
    "go developer": "Backend Developer",
    "java developer": "Backend Developer",
    "node.js developer": "Backend Developer",
    "ruby developer": "Backend Developer",
    "rust developer": "Backend Developer",
    "integration engineer": "Backend Developer",
    "frontend engineer": "Frontend Developer",
    "frontend dev": "Frontend Developer",
    "react developer": "Frontend Developer",
    "vue developer": "Frontend Developer",
    "angular developer": "Frontend Developer",
    "full stack engineer": "Full Stack Developer",
    "full-stack developer": "Full Stack Developer",
    "fullstack developer": "Full Stack Developer",
    "web developer": "Full Stack Developer",
    "mobile developer": "Mobile Developer",
    "ios developer": "Mobile Developer",
    "android developer": "Mobile Developer",
    "flutter developer": "Mobile Developer",
    "react native developer": "Mobile Developer",
    "blockchain engineer": "Blockchain Developer",
    "solidity developer": "Blockchain Developer",
    "smart contract developer": "Smart Contract Developer",
    "smart contract engineer": "Smart Contract Developer",
    "defi engineer": "Blockchain Developer",
    "web3 developer": "Blockchain Developer",
    "devops": "DevOps Engineer",
    "sre": "DevOps Engineer",
    "site reliability engineer": "DevOps Engineer",
    "infrastructure engineer": "DevOps Engineer",
    "platform engineer": "DevOps Engineer",
    "cloud engineer": "DevOps Engineer",
    "qa": "QA Engineer",
    "tester": "QA Engineer",
    "qa automation engineer": "QA Engineer",
    "qa backend engineer": "QA Engineer",
    "manual qa": "QA Engineer",
    "qa lead": "QA Engineer",
    "test engineer": "QA Engineer",
    "security architect": "Security Engineer",
    "lead security architect": "Security Engineer",
    "cybersecurity": "Security Engineer",
    "information security": "Security Engineer",
    "pentest": "Security Engineer",
    "administrator": "System Administrator",
    "sysadmin": "System Administrator",
    "data analyst": "Data Analyst",
    "operational data analyst": "Data Analyst",
    "product analyst": "Data Analyst",
    "analyst": "Data Analyst",
    "bi analyst": "Data Analyst",
    "hr analyst": "Data Analyst",
    "research analyst": "Data Analyst",
    "researcher": "Data Analyst",
    "risk analyst": "Risk Analyst",
    "payments risk analyst": "Risk Analyst",
    "data engineer": "Data Engineer",
    "data scientist": "Data Scientist",
    "machine learning engineer": "ML Engineer",
    "ml engineer": "ML Engineer",
    "mlops specialist": "ML Engineer",
    "llm engineer": "ML Engineer",
    "ai specialist": "ML Engineer",
    "ai engineer": "ML Engineer",
    "product manager": "Product Manager",
    "growth product manager": "Product Manager",
    "product marketing manager": "Marketing Manager",
    "product specialist": "Product Manager",
    "project manager": "Project Manager",
    "project coordinator": "Project Manager",
    "payments project manager": "Project Manager",
    "producer": "Project Manager",
    "product owner": "Product Owner",
    "business analyst": "Business Analyst",
    "system analyst": "System Analyst",
    "lead system analyst": "System Analyst",
    "ui/ux designer": "UI/UX Designer",
    "ux/ui designer": "UI/UX Designer",
    "product designer": "UI/UX Designer",
    "middle product designer": "UI/UX Designer",
    "ux designer": "UI/UX Designer",
    "ui designer": "UI/UX Designer",
    "designer": "UI/UX Designer",
    "graphic designer": "Graphic Designer",
    "motion designer": "Motion Designer",
    "animator": "Motion Designer",
    "3d animator": "3D Artist",
    "3d artist": "3D Artist",
    "game designer": "Game Designer",
    "game mathematician": "Game Designer",
    "marketing manager": "Marketing Manager",
    "marketing specialist": "Marketing Manager",
    "marketing lead": "Marketing Manager",
    "marketing assistant": "Marketing Manager",
    "marketing": "Marketing Manager",
    "performance marketer": "Performance Marketing Manager",
    "performance marketing specialist": "Performance Marketing Manager",
    "performance creative": "Performance Marketing Manager",
    "campaign manager": "Performance Marketing Manager",
    "media buyer": "Media Buyer",
    "ppc buyer": "Media Buyer",
    "ppc specialist": "Media Buyer",
    "media buyer / team lead": "Media Buyer",
    "team lead media buyer": "Media Buyer",
    "seo specialist": "SEO Specialist",
    "seo team lead": "SEO Specialist",
    "seo manager": "SEO Specialist",
    "smm specialist": "SMM Manager",
    "smm manager": "SMM Manager",
    "smm": "SMM Manager",
    "content manager": "Content Manager",
    "content creator": "Content Manager",
    "content specialist": "Content Manager",
    "content lead": "Content Manager",
    "content brand manager": "Content Manager",
    "review writer": "Content Manager",
    "copywriter": "Content Manager",
    "editor": "Content Manager",
    "community manager": "Community Manager",
    "head of support & community": "Community Manager",
    "traffic handler": "Traffic Manager",
    "traffic manager": "Traffic Manager",
    "traffic specialist": "Traffic Manager",
    "affiliate manager": "Affiliate Manager",
    "affiliate": "Affiliate Manager",
    "growth manager": "Growth Manager",
    "head of growth": "Growth Manager",
    "go-to-market champion": "Growth Manager",
    "growth lead": "Growth Manager",
    "crm manager": "CRM Manager",
    "head of crm": "CRM Manager",
    "retention manager": "CRM Manager",
    "head of retention": "CRM Manager",
    "pr manager": "PR Manager",
    "influencer manager": "PR Manager",
    "sales manager": "Sales Manager",
    "sales": "Sales Manager",
    "sales representative": "Sales Manager",
    "account farmer": "Sales Manager",
    "agent": "Sales Manager",
    "business development manager": "Business Development Manager",
    "business development": "Business Development Manager",
    "bd manager": "Business Development Manager",
    "partnerships specialist/manager": "Partnerships Manager",
    "partnerships manager": "Partnerships Manager",
    "account manager": "Account Manager",
    "financial manager": "Financial Manager",
    "head of finance": "Financial Manager",
    "investment manager": "Financial Manager",
    "compliance manager": "Compliance Manager",
    "head of compliance": "Compliance Manager",
    "head of anti-fraud": "Compliance Manager",
    "aml specialist": "Compliance Manager",
    "legal counsel": "Legal Counsel",
    "legal": "Legal Counsel",
    "hr manager": "HR Manager",
    "hr specialist": "HR Manager",
    "hr operations specialist": "HR Manager",
    "hr business partner": "HR Manager",
    "hr manager / lead recruiter": "HR Manager",
    "recruiter": "Recruiter",
    "recruitment agent": "Recruiter",
    "lead recruiter": "Recruiter",
    "customer support": "Customer Support",
    "support specialist": "Customer Support",
    "helpdesk specialist": "Customer Support",
    "head of support": "Customer Support",
    "operations manager": "Operations Manager",
    "head of operations": "Operations Manager",
    "operations specialist": "Operations Manager",
    "payment operations specialist": "Operations Manager",
    "director of exchange operations": "Operations Manager",
    "payments lead": "Operations Manager",
    "global card scheme manager": "Operations Manager",
    "internal audit it manager": "Operations Manager",
    "p2p exchange specialist": "Operations Manager",
    "personal assistant": "Operations Manager",
    "chief product officer": "Executive",
    "chief operating officer": "Executive",
    "chief technology officer": "Executive",
    "managing director": "Executive",
    "cto": "Executive",
    "cpo": "Executive",
    "ceo": "Executive",
    "coo": "Executive",
    "cfo": "Executive",
    "cmo": "Executive",
    "executive": "Executive",
    "c-level": "Executive",
    "lead creative": "Graphic Designer",
    "tech lead": "Backend Developer",
    "team lead": "Other",
    "developer": "Full Stack Developer",
    "engineer": "Full Stack Developer",
    "specialist": "Other",
    "technical specialist": "Other",
    "management": "Operations Manager",
    "manager": "Operations Manager",
    "head": "Other",
    "data": "Data Analyst",
    "developer relations engineer": "Community Manager",
}

_SENIORITY_STRIP_RE = re.compile(
    r"^(trainee|intern|junior\+?|middle|mid(?:-level)?|senior|sr\.?|lead|head|chief|principal|staff)\s+",
    re.I,
)


def normalize_role(raw: str | None) -> str | None:
    """Normalize a free-text role to the canonical taxonomy."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    lc = s.lower()

    if lc in _ROLE_ALIAS:
        return _ROLE_ALIAS[lc]

    if lc in _ROLE_CANONICAL_LOWER:
        return _ROLE_CANONICAL_LOWER[lc]

    stripped = _SENIORITY_STRIP_RE.sub("", s).strip()
    if stripped:
        stripped_lc = stripped.lower()
        if stripped_lc in _ROLE_ALIAS:
            return _ROLE_ALIAS[stripped_lc]
        if stripped_lc in _ROLE_CANONICAL_LOWER:
            return _ROLE_CANONICAL_LOWER[stripped_lc]

    for alias_key, canonical in _ROLE_ALIAS.items():
        if alias_key in lc or lc in alias_key:
            return canonical

    return s.title() if len(s) > 2 else None


def extract_seniority_from_title(title: str | None) -> str | None:
    """Try to extract seniority from a job title string."""
    if not title:
        return None
    m = _SENIORITY_PREFIX_RE.match(title.strip())
    if m:
        return normalize_seniority(m.group(1))
    for kw in ("team lead", "teamlead", "tech lead"):
        if kw in title.lower():
            return "lead"
    for kw in ("chief", "cto", "cpo", "ceo", "coo", "cfo"):
        if kw in title.lower().split():
            return "c-level"
    for kw in ("head of", "director of", "vp of"):
        if kw in title.lower():
            return "head"
    return None


def normalize_vacancy_fields(
    role: str | None,
    seniority: str | None,
    title: str | None = None,
    standardized_title: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Return (normalized_role, normalized_seniority).
    Tries to infer missing seniority from title if not explicitly provided.
    """
    norm_seniority = normalize_seniority(seniority)
    norm_role = normalize_role(role)

    if not norm_seniority:
        norm_seniority = extract_seniority_from_title(
            standardized_title or title
        )

    return norm_role, norm_seniority
