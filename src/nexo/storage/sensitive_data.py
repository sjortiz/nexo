"""Pluggable PII/PHI detection used at ingestion and in the compliance node.

`RegexDetector` is a dependency-free baseline. `ComprehendMedicalDetector`
calls Amazon Comprehend Medical DetectPHI; it sends text to an AWS regional
endpoint, so enable it only where that data flow is approved (BAA, region).
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Protocol

from nexo.storage.schemas import Sensitivity


@dataclass(frozen=True)
class Detection:
    """One sensitive span found in a text, with its category and level."""

    category: str
    begin: int
    end: int
    sensitivity: Sensitivity


class SensitiveDataDetector(Protocol):  # pylint: disable=too-few-public-methods
    """Finds PII/PHI spans in text."""

    def detect(self, text: str) -> list[Detection]:
        """Return the sensitive spans found in the text."""


class NoopDetector:  # pylint: disable=too-few-public-methods  # one-method protocol
    """Detector that finds nothing, for when detection is turned off."""

    def detect(self, text: str) -> list[Detection]:  # pylint: disable=unused-argument
        """Return no detections; text is accepted only to match the protocol."""
        return []


class RegexDetector:  # pylint: disable=too-few-public-methods  # one-method protocol
    """High-precision patterns only; recall is the job of a real PHI service."""

    _PATTERNS: tuple[tuple[str, re.Pattern[str], Sensitivity], ...] = (
        ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), Sensitivity.PII),
        ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), Sensitivity.PII),
        ("DO_CEDULA", re.compile(r"\b\d{3}-\d{7}-\d\b"), Sensitivity.PII),
        (
            "PHONE",
            re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"),
            Sensitivity.PII,
        ),
        ("PAYMENT_CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b"), Sensitivity.PII),
        (
            "MRN",
            re.compile(
                r"\b(?:MRN|medical record (?:number|no\.?))[:#\s]*[A-Z0-9-]{5,}\b", re.I
            ),
            Sensitivity.PHI,
        ),
    )

    def detect(self, text: str) -> list[Detection]:
        """Return matches of the built-in patterns; card numbers must pass Luhn."""
        found: list[Detection] = []
        for category, pattern, sensitivity in self._PATTERNS:
            for match in pattern.finditer(text):
                if category == "PAYMENT_CARD" and not _luhn_ok(match.group()):
                    continue
                found.append(
                    Detection(category, match.start(), match.end(), sensitivity)
                )
        return found


class ComprehendMedicalDetector:  # pylint: disable=too-few-public-methods  # one-method protocol
    """Amazon Comprehend Medical DetectPHI (20,000 UTF-8 bytes per call)."""

    _MAX_CHARS = 18_000  # stay under the byte limit for mostly-ASCII text

    def __init__(self, region_name: str, min_score: float = 0.5) -> None:
        try:  # optional extra: nexo[aws]
            boto3 = importlib.import_module("boto3")
        except ModuleNotFoundError as exc:
            raise ValueError(
                "Comprehend Medical detection needs the extra: nexo[aws]"
            ) from exc
        self._client = boto3.client("comprehendmedical", region_name=region_name)
        self._min_score = min_score

    def detect(self, text: str) -> list[Detection]:
        """Return PHI entities at or above the score threshold, in batches under the limit."""
        found: list[Detection] = []
        for offset in range(0, len(text), self._MAX_CHARS):
            response = self._client.detect_phi(
                Text=text[offset : offset + self._MAX_CHARS]
            )
            for entity in response.get("Entities", []):
                if entity.get("Score", 0.0) < self._min_score:
                    continue
                begin, end = (
                    offset + entity["BeginOffset"],
                    offset + entity["EndOffset"],
                )
                found.append(Detection(entity["Type"], begin, end, Sensitivity.PHI))
        return found


def classify(detections: list[Detection], floor: Sensitivity) -> Sensitivity:
    """Highest of the document's declared sensitivity and anything detected."""
    return max([floor, *(d.sensitivity for d in detections)], key=lambda s: s.rank)


def redact(text: str, detections: list[Detection]) -> str:
    """Replace each detected span with its [CATEGORY] placeholder."""
    for d in sorted(detections, key=lambda d: d.begin, reverse=True):
        text = f"{text[: d.begin]}[{d.category}]{text[d.end :]}"
    return text


def build_detector(
    provider: str, *, aws_region: str | None = None
) -> SensitiveDataDetector:
    """Return the detector for a provider name: none, regex or comprehend_medical."""
    if provider == "none":
        return NoopDetector()
    if provider == "regex":
        return RegexDetector()
    if provider == "comprehend_medical":
        if not aws_region:
            raise ValueError("comprehend_medical requires AWS_REGION")
        return ComprehendMedicalDetector(aws_region)
    raise ValueError(f"unknown sensitive-data provider {provider!r}")


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    checksum = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2:
            digit *= 2
            digit -= 9 if digit > 9 else 0
        checksum += digit
    return len(digits) >= 13 and checksum % 10 == 0
