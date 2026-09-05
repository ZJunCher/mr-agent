"""
fetch_coverage_report 工具 - 从 x86_64_ut_coverage_check job 拉取
`coverage_html/changed_lines.html` artifact，解析出未覆盖行报告，
转成紧凑文本喂给 plan_fix。

设计要点
--------
- 走 python-gitlab API（`project.jobs.get(job_id).artifact(path)`），
  不拼裸 URL，鉴权统一由 git_provider 处理。
- 用 stdlib `html.parser`（不引新依赖）做轻量预提取：抓 (file, line_no, code, is_uncovered)。
- 红色判定容忍多种实现：class 含 `uncov|miss|red|no-cov`，或 inline style 含 `background` + 红/粉色。
- 连续未覆盖行号合并成 range。
- 解析失败时回落到"压缩 HTML 直接喂 LLM"，仍可用。
"""
from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Annotated, Optional

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from pr_agent.triage.pipeline_coverage import ARTIFACT_PATH, parse_changed_lines_summary
from ut_agent.tools.context import get_git_provider

logger = logging.getLogger("ut_agent")

# 用于检测"未覆盖"行的标记（class 名）
# 注意：用前缀匹配（不锁右侧 \b），这样 "uncovered" / "missed" / "nocov-line" 都能命中。
_UNCOV_CLASS_RE = re.compile(r"\b(uncov|miss|nocov|no-cov|red)", re.IGNORECASE)
_COV_CLASS_RE = re.compile(r"\b(cov(ered)?|hit|green)\b", re.IGNORECASE)

# 颜色名 -> RGB
_NAMED_COLORS = {
    "red": (255, 0, 0), "pink": (255, 192, 203), "lightpink": (255, 182, 193),
    "salmon": (250, 128, 114), "lightsalmon": (255, 160, 122), "crimson": (220, 20, 60),
    "mistyrose": (255, 228, 225), "lavenderblush": (255, 240, 245),
    "green": (0, 128, 0), "lightgreen": (144, 238, 144), "lime": (0, 255, 0),
    "palegreen": (152, 251, 152), "honeydew": (240, 255, 240),
    "yellow": (255, 255, 0), "lightyellow": (255, 255, 224),
    "white": (255, 255, 255), "transparent": (255, 255, 255),
}

_BG_IN_STYLE_RE = re.compile(r"background(?:-color)?\s*:\s*([^;]+)", re.IGNORECASE)
_HEX_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
_RGB_RE = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", re.IGNORECASE)
_FILE_PATH_RE = re.compile(r"[\w./\-]+\.(?:c|cc|cpp|cxx|h|hpp|hh|py)\b")


def _parse_color(text: str) -> Optional[tuple[int, int, int]]:
    """从一段文本里抽出第一个能识别的颜色，返回 (r,g,b)。识别 #hex / rgb() / 颜色名。"""
    if not text:
        return None
    m = _HEX_RE.search(text)
    if m:
        h = m.group(1)
        if len(h) == 3:
            return (int(h[0]*2, 16), int(h[1]*2, 16), int(h[2]*2, 16))
        if len(h) >= 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = _RGB_RE.search(text)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    low = text.lower()
    for name, rgb in _NAMED_COLORS.items():
        if re.search(rf"\b{name}\b", low):
            return rgb
    return None


def _classify_color(rgb: tuple[int, int, int]) -> str:
    """把一个 RGB 归类为 'red' / 'green' / 'neutral'。
    判定逻辑：
      - red:   R 明显高于 G 且 R 明显高于 B（R-G >= 25 且 R-B >= 25），且不是接近白色的浅灰
      - green: G 明显高于 R（G-R >= 25），且 G 明显高于 B 或接近
      - 其余视作中性（白/灰/黄等）
    """
    r, g, b = rgb
    # 接近纯白：忽略
    if min(r, g, b) >= 240:
        return "neutral"
    if r - g >= 25 and r - b >= 0:
        return "red"
    if g - r >= 25:
        return "green"
    return "neutral"


def _bg_color_from_attrs(d: dict[str, str]) -> Optional[tuple[int, int, int]]:
    """综合 style="background:..." 和 bgcolor="..." 抽出 bg 颜色。"""
    style = d.get("style", "")
    if style:
        m = _BG_IN_STYLE_RE.search(style)
        if m:
            c = _parse_color(m.group(1))
            if c:
                return c
    bg = d.get("bgcolor", "")
    if bg:
        c = _parse_color(bg)
        if c:
            return c
    return None


class _ChangedLinesParser(HTMLParser):
    """轻量 HTML 解析器：识别每个文件块下"未覆盖"的行号 + 代码片段。

    判定一行是否未覆盖的优先级（任一命中即视作未覆盖）：
      1) 该行（或其祖先 row 元素）class 命中 _UNCOV_CLASS_RE
      2) 该行（或其祖先）style/bgcolor 解析出"红"色
      3) 兜底：若同页存在大量绿色行（即"覆盖"用绿色标），则任何"非绿、非中性、非空"的 row
         也视作未覆盖（在 finalize 阶段做反向推断）
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.summary: dict = {}
        self.files: list[dict] = []
        # 全部 row 候选（无论颜色），后处理可做反向分类
        # 每项: {"path": str|None, "line": int, "code": str, "signal": "red"|"green"|"neutral"}
        self._all_rows: list[dict] = []
        # 颜色信号统计（用于反向推断兜底）
        self._color_stats = {"red": 0, "green": 0, "neutral": 0}

        self._current_file: Optional[dict] = None
        self._stack: list[tuple[str, dict]] = []
        self._row_signal: str = "neutral"  # red / green / neutral
        self._row_text_chunks: list[str] = []
        self._in_row: bool = False

    @staticmethod
    def _attr_to_dict(attrs: list[tuple[str, Optional[str]]]) -> dict[str, str]:
        return {k: (v or "") for k, v in attrs}

    @classmethod
    def _row_signal_from_attrs(cls, d: dict[str, str]) -> str:
        cls_attr = d.get("class", "")
        if cls_attr and _UNCOV_CLASS_RE.search(cls_attr):
            return "red"
        if cls_attr and _COV_CLASS_RE.search(cls_attr):
            return "green"
        rgb = _bg_color_from_attrs(d)
        if rgb:
            return _classify_color(rgb)
        return "neutral"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        d = self._attr_to_dict(attrs)
        self._stack.append((tag, d))

        # 行级元素：tr / div(line) / li / pre 任何一个都可能是一行
        if tag in ("tr", "div", "li", "pre") and not self._in_row:
            sig = self._row_signal_from_attrs(d)
            # 只要这个块"看起来"是一行（有颜色信号 或 父级是 table/code 容器），就开始采集
            if sig != "neutral" or tag == "tr":
                self._in_row = True
                self._row_signal = sig
                self._row_text_chunks = []
                return

        # 在 row 内嵌套的 td/span 也可能携带颜色信号 → 升级当前 row 的 signal
        if self._in_row and self._row_signal == "neutral":
            sig = self._row_signal_from_attrs(d)
            if sig != "neutral":
                self._row_signal = sig

    def handle_endtag(self, tag: str) -> None:
        if self._in_row and tag in ("tr", "div", "li", "pre"):
            text = " ".join(c.strip() for c in self._row_text_chunks if c.strip())
            if text:
                self._consume_row(text, self._row_signal)
            self._in_row = False
            self._row_signal = "neutral"
            self._row_text_chunks = []
        if self._stack and self._stack[-1][0] == tag:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_row:
            self._row_text_chunks.append(data)
            return
        # 不在 row 中：检测文件路径
        if data.strip():
            m = _FILE_PATH_RE.search(data)
            if m:
                self._switch_file(m.group(0).strip())

    def _switch_file(self, path: str) -> None:
        if self._current_file and self._current_file["path"] == path:
            return
        existing = next((f for f in self.files if f["path"] == path), None)
        if existing:
            self._current_file = existing
        else:
            self._current_file = {"path": path, "uncovered": []}
            self.files.append(self._current_file)

    def _consume_row(self, text: str, signal: str) -> None:
        m = re.match(r"^\s*(\d{1,6})\s+(.*)$", text)
        if not m:
            return
        line_no = int(m.group(1))
        code = m.group(2).rstrip()
        path = self._current_file["path"] if self._current_file else "<unknown>"
        self._all_rows.append({"path": path, "line": line_no, "code": code, "signal": signal})
        self._color_stats[signal] = self._color_stats.get(signal, 0) + 1
        if signal == "red":
            if not self._current_file:
                self._current_file = {"path": "<unknown>", "uncovered": []}
                self.files.append(self._current_file)
            self._current_file["uncovered"].append({"line": line_no, "code": code})

    def finalize(self) -> None:
        """后处理：如果 class/红色都没识别到任何 uncovered，但抓到了大量行 +
        摘要里写明 uncovered>0，就启用反向兜底——把"非绿、非中性、看起来非空"
        的行也算未覆盖。这一步只在主路径 0 命中时启用，避免误伤。"""
        red_count = self._color_stats.get("red", 0)
        if red_count > 0:
            return
        expected = self.summary.get("uncovered", 0) or 0
        if expected <= 0 or not self._all_rows:
            return
        # 反向推断：把 signal == "neutral" 但代码非空、且页面整体存在 green 主导的 row 都标红
        green_count = self._color_stats.get("green", 0)
        if green_count == 0:
            return
        # 简单策略：把所有 neutral row 当作 uncovered 候选（以摘要数量为上限）
        candidates = [r for r in self._all_rows if r["signal"] == "neutral" and r["code"].strip()]
        # 文件分组重建 uncovered
        for r in candidates[:expected]:
            target = next((f for f in self.files if f["path"] == r["path"]), None)
            if not target:
                target = {"path": r["path"] or "<unknown>", "uncovered": []}
                self.files.append(target)
            target["uncovered"].append({"line": r["line"], "code": r["code"]})


def _group_consecutive(rows: list[dict]) -> list[dict]:
    """把 [{line, code}] 按连续行号分组成 [{start, end, codes:[..]}]"""
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r["line"])
    groups: list[dict] = []
    cur = {"start": rows[0]["line"], "end": rows[0]["line"], "codes": [rows[0]["code"]]}
    for r in rows[1:]:
        if r["line"] == cur["end"] + 1:
            cur["end"] = r["line"]
            cur["codes"].append(r["code"])
        else:
            groups.append(cur)
            cur = {"start": r["line"], "end": r["line"], "codes": [r["code"]]}
    groups.append(cur)
    return groups


def _render_report(parsed: dict, max_files: int = 20, max_lines_per_file: int = 80) -> str:
    """把解析结果渲染成给 LLM 看的紧凑文本。"""
    lines: list[str] = []
    summary = parsed.get("summary") or {}
    if summary:
        parts = []
        if "total" in summary:
            parts.append(f"总修改行 {summary['total']}")
        if "covered" in summary:
            parts.append(f"已覆盖 {summary['covered']}")
        if "uncovered" in summary:
            parts.append(f"未覆盖 {summary['uncovered']}")
        if "coverage_pct" in summary:
            parts.append(f"覆盖率 {summary['coverage_pct']}%")
        if parts:
            lines.append("[覆盖率摘要] " + " | ".join(parts))
            lines.append("")

    files = parsed.get("files") or []
    rendered_files = 0
    for f in files:
        uncov = f.get("uncovered") or []
        if not uncov:
            continue
        if rendered_files >= max_files:
            lines.append(f"... 还有 {len(files) - rendered_files} 个文件未列出")
            break
        groups = _group_consecutive(uncov)
        lines.append(f"文件: {f['path']} (未覆盖 {len(uncov)} 行)")
        emitted = 0
        for g in groups:
            if emitted >= max_lines_per_file:
                lines.append(f"  ... 还有 {len(uncov) - emitted} 行未列出")
                break
            rng = f"L{g['start']}" if g["start"] == g["end"] else f"L{g['start']}-{g['end']}"
            lines.append(f"  - {rng}:")
            for i, code in enumerate(g["codes"]):
                if emitted >= max_lines_per_file:
                    break
                ln = g["start"] + i
                lines.append(f"      {ln:>5} | {code}")
                emitted += 1
        lines.append("")
        rendered_files += 1
    return "\n".join(lines).strip() or "（解析未发现未覆盖行）"


def _try_parse_summary(html: str) -> dict:
    """从 HTML 顶部抓总修改、已覆盖、未覆盖和覆盖率数字。"""
    return parse_changed_lines_summary(html)


def fetch_changed_lines_report(ut_job_id: int) -> dict:
    """
    拉取 x86_64_ut_coverage_check 的 changed_lines.html artifact 并解析。

    返回:
        {
            "available": True/False,
            "reason": str,                # 失败时的原因
            "report_text": str,           # 给 LLM 用的紧凑文本
            "raw_html_compact": str,      # 兜底：剥掉 style/script 的精简 HTML
            "summary": {...},
            "files": [...],
        }
    """
    git_provider = get_git_provider()
    if not git_provider:
        return {"status": "unknown", "available": False, "reason": "git_provider 未初始化"}

    try:
        gl = git_provider.gl
        proj_id = git_provider.id_project
        project = gl.projects.get(proj_id)
    except Exception as e:
        return {"status": "unknown", "available": False, "reason": f"获取项目失败: {e}"}

    try:
        job = project.jobs.get(ut_job_id)
    except Exception as e:
        return {"status": "unknown", "available": False, "reason": f"获取 job #{ut_job_id} 失败: {e}"}

    try:
        raw = job.artifact(ARTIFACT_PATH)
    except Exception as e:
        return {
            "status": "unknown",
            "available": False,
            "reason": f"拉取 artifact {ARTIFACT_PATH} 失败: {e}",
        }

    if isinstance(raw, bytes):
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("utf-8", errors="replace")
    else:
        html = str(raw)

    logger.info(f"[coverage] 拉取 changed_lines.html: {len(html)} chars")

    # 解析
    parser = _ChangedLinesParser()
    parser.summary = _try_parse_summary(html)
    try:
        parser.feed(html)
        parser.close()
        parser.finalize()
    except Exception as e:
        logger.warning(f"[coverage] HTML 解析异常: {e}")

    parsed = {
        "summary": parser.summary,
        "files": [
            {"path": f["path"], "uncovered": f["uncovered"]}
            for f in parser.files
            if f.get("uncovered")
        ],
        "color_stats": parser._color_stats,
    }

    report_text = _render_report(parsed)
    logger.info(
        f"[coverage] 解析结果: files={len(parsed['files'])} "
        f"color_stats={parser._color_stats} summary={parsed['summary']}"
    )

    # 兜底：剥 style/script 的精简 HTML（限 16KB）
    compact = re.sub(r"<style[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
    compact = re.sub(r"<script[\s\S]*?</script>", "", compact, flags=re.IGNORECASE)
    compact = re.sub(r"\s+", " ", compact).strip()
    if len(compact) > 16000:
        compact = compact[:16000] + "...<truncated>"

    return {
        "status": "success",
        "available": True,
        "reason": "",
        "report_text": report_text,
        "raw_html_compact": compact,
        "summary": parsed["summary"],
        "files": parsed["files"],
        "color_stats": parser._color_stats,
    }


# ── ReAct Agent 工具包装 ──


@tool
def fetch_coverage_report_tool(
    job_id: int,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """拉取覆盖率报告，返回未覆盖行清单。

    从 x86_64_ut_coverage_check job 的 artifact 中解析 changed_lines.html，
    返回结构化的未覆盖行号清单（按文件分组）。

    参数:
        job_id: x86_64_ut_coverage_check 的 job ID

    返回: 未覆盖行报告文本，或错误描述。
    """
    if not job_id:
        return json.dumps({
            "status": "unknown",
            "available": False,
            "message": "无 job_id，请先从流水线结果中获取",
        }, ensure_ascii=False)

    result = fetch_changed_lines_report(job_id)
    if result.get("available"):
        result["message"] = result.get("report_text", "（解析未发现未覆盖行）")
    else:
        result["message"] = f"覆盖率报告状态未知: {result.get('reason', '未知')}"
    return json.dumps(result, ensure_ascii=False)
