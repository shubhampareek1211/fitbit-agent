"""Deterministic Verifier: the runtime critic for the Writer, and a scorer in the offline evals.

Checks a report against the EvidenceLedger:
  1. every cited id ([E3]) exists;
  2. every number in a cited sentence matches a value in one of its cited evidence items
     (allowing rounding and minutes<->hours conversions);
  3. numbers outside the limitations section are cited;
  4. a "what your data can't tell you" section is present;
  5. no diagnostic language.
"""
import re

from pulse.evidence import EvidenceLedger
from pulse.schemas import VerifyResult

CITE = re.compile(r"\[(E\d+(?:\s*[,;]\s*E\d+)*)\]")  # [E3] or [E3, E7]


def _cited(sentence: str) -> list[str]:
    return [i for block in CITE.findall(sentence) for i in re.split(r"\s*[,;]\s*", block)]
NUMBER = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
DATE = re.compile(  # ISO dates, ISO months, "Nov 7(, 2026)", and "November 2026" (eval v2 found this false positive)
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{4}}-\d{{2}}\b|\b{_MONTH} \d{{1,2}}(?:st|nd|rd|th)?\b(?:,? \d{{4}})?"
    rf"|\b{_MONTH},? \d{{4}}\b", re.I)
# Clock times ("19:01", "7:30 pm", "7 pm") and heart-rate zone names ("Zone 2") are labels, not data to check.
TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?(?![\w:])|\b\d{1,2}\s*[ap]\.?m\.?(?![a-z])", re.I)
ZONE_NAME = re.compile(r"\bzone\s*\d\b", re.I)
LIMITS_HEADING = re.compile(r"can['’]?t tell|cannot tell|can not tell|limitations", re.I)
DIAGNOSIS = re.compile(
    r"\byou (?:likely |probably |may |might )?have (?:a |an )?(?:sleep apnea|apnea|insomnia|arrhythmia|"
    r"atrial fibrillation|afib|disease|disorder|condition|diabetes|hypertension)\b|\bdiagnos(?:e|is|ed) (?:you|with)\b"
    r"|\byou should (?:take|start|stop) (?:medication|medicine|pills)\b", re.I)
SOURCES_HEADING = re.compile(r"^#+\s*sources\b", re.I | re.M)


def _sentences(text: str) -> list[str]:
    parts = []
    for line in text.splitlines():
        parts += re.split(r"(?<=[.!?])\s+(?=[A-Z(*_])", line)
    return [p for p in parts if p.strip()]


def _numbers(sentence: str) -> list[tuple[float, int]]:
    """Numbers as (value, decimals), ignoring dates, clock times, zone names, citation ids and list markers."""
    s = CITE.sub(" ", ZONE_NAME.sub(" ", TIME.sub(" ", DATE.sub(" ", sentence))))
    s = re.sub(r"^\s*(?:\d+[.)]|[-*])\s+", " ", s)  # list markers
    out = []
    for m in NUMBER.finditer(s):
        raw = m.group().replace(",", "")
        dec = len(raw.split(".")[1]) if "." in raw else 0
        out.append((float(raw), dec))
    return out


def _candidates(v: float) -> list[float]:
    return [v, abs(v), v / 60, v * 60, v % 60, v / 1000, v * 100, v / 100]


def _matches(n: float, dec: int, values: list[float]) -> bool:
    tol = max(0.5 * 10 ** -dec, 0.006 * abs(n))
    return any(abs(c - n) <= tol for v in values for c in _candidates(v))


def verify(report: str, ledger: EvidenceLedger) -> VerifyResult:
    problems: list[str] = []
    body = SOURCES_HEADING.split(report)[0]
    if not LIMITS_HEADING.search(body):
        problems.append('Missing a "What your data can\'t tell you" section.')
    if m := DIAGNOSIS.search(body):
        problems.append(f'Diagnostic language is not allowed: "{m.group()}". Describe data, not conditions.')

    cited_all, checked = [], 0
    in_limits = False
    for sent in _sentences(body):
        if sent.lstrip().startswith("#"):
            in_limits = bool(LIMITS_HEADING.search(sent))
            continue
        ids = _cited(sent)
        nums = _numbers(sent)
        cited_all += ids
        unknown = [i for i in ids if ledger.get(i) is None]
        if unknown:
            problems.append(f"Unknown citation(s) {unknown} in: \"{sent.strip()[:120]}\"")
        if not nums:
            continue
        if not ids:
            if not in_limits:
                problems.append(f"Number(s) without a citation: \"{sent.strip()[:120]}\"")
            continue
        values = [v for i in ids if (e := ledger.get(i)) for v in e.values.values() if v is not None]
        values += [float(e.n) for i in ids if (e := ledger.get(i)) and e.n is not None]
        for n, dec in nums:
            checked += 1
            if not _matches(n, dec, values):
                problems.append(f"{n:g} is not in the cited evidence {ids}: \"{sent.strip()[:120]}\"")
    return VerifyResult(passed=not problems, problems=problems[:12],
                        cited_ids=sorted(set(cited_all), key=lambda x: int(x[1:])), numbers_checked=checked)
