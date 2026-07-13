import re


HIGH_RISK_TERMS = {
    "auth",
    "authorization",
    "permission",
    "cryptography",
    "crypto",
    "payment",
    "billing",
    "secret",
    "token",
    "credential",
    "production",
    "database",
    "migration",
}
MEDIUM_RISK_TERMS = {"refactor", "api", "schema", "data", "bulk", "concurrent", "parallel"}
RISK_RANKS = {"low": 0, "medium": 1, "high": 2}


def risk_for_text(text: str) -> str:
    lowered = text.lower()
    if _contains_term(lowered, HIGH_RISK_TERMS):
        return "high"
    if _contains_term(lowered, MEDIUM_RISK_TERMS):
        return "medium"
    return "medium"


def _contains_term(text: str, terms) -> bool:
    return any(
        re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(term), text) is not None
        for term in terms
    )


def max_risk(*risks: str) -> str:
    selected = "low"
    for risk in risks:
        if RISK_RANKS.get(risk, -1) > RISK_RANKS[selected]:
            selected = risk
    return selected
