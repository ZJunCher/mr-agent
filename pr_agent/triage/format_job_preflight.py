import re
from dataclasses import asdict, dataclass

FORMAT_REPAIRABLE_OR_UNKNOWN = "repairable_or_unknown"
FORMAT_CI_JOB_CONFIGURATION = "ci_job_configuration"
_EVIDENCE_LIMIT = 500
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_EMPTY_DIFF_REVISION_RE = re.compile(
    r"git\s+diff\s+failed\s*:\s*fatal\s*:\s*ambiguous\s+argument\s+(['\"])\1",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FormatJobDisposition:
    kind: str = FORMAT_REPAIRABLE_OR_UNKNOWN
    summary: str = ""
    evidence: str = ""
    suggested_action: str = ""
    job_url: str = ""

    @property
    def repairable(self) -> bool:
        return self.kind != FORMAT_CI_JOB_CONFIGURATION

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _sanitize_line(line: str) -> str:
    return " ".join(_ANSI_ESCAPE_RE.sub("", line).strip().split())


def classify_format_job_trace(trace: str, *, job_url: str = "") -> FormatJobDisposition:
    """Classify only exact, high-confidence Format Job execution failures."""
    for line in str(trace or "").splitlines():
        sanitized = _sanitize_line(line)
        match = _EMPTY_DIFF_REVISION_RE.search(sanitized)
        if not match:
            continue
        evidence_start = max(0, match.start() - 100)
        return FormatJobDisposition(
            kind=FORMAT_CI_JOB_CONFIGURATION,
            summary="CI 传给 git diff 的基准 Commit 为空，格式检查尚未开始，因此没有可应用的格式报告。",
            evidence=sanitized[evidence_start:evidence_start + _EVIDENCE_LIMIT],
            suggested_action="请修正 CI 模板中的 diff 基准变量后重新运行流水线。",
            job_url=str(job_url or ""),
        )
    return FormatJobDisposition(job_url=str(job_url or ""))
