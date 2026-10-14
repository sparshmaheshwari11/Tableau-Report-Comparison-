"""
modules/workbook_comparator.py
-------------------------------
Step 1 — Workbook-level metadata comparison.
Compares project, owner, dates, tags, and top-level attributes.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import tableauserverclient as TSC

from modules.tableau_client import TableauClient
from modules.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class WorkbookMeta:
    id: str
    name: str
    content_url: str
    project_name: str
    project_id: str
    owner_id: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    description: str
    tags: List[str]
    show_tabs: bool
    size_bytes: int
    webpage_url: str


@dataclass
class WorkbookDiff:
    field: str
    source_value: Any
    target_value: Any
    severity: str  # INFO | WARNING | ERROR


def extract_workbook_meta(workbook: TSC.WorkbookItem) -> WorkbookMeta:
    """Map a TSC WorkbookItem to our flat WorkbookMeta dataclass."""
    return WorkbookMeta(
        id=workbook.id or "",
        name=workbook.name or "",
        content_url=workbook.content_url or "",
        project_name=workbook.project_name or "",
        project_id=workbook.project_id or "",
        owner_id=workbook.owner_id or "",
        created_at=workbook.created_at,
        updated_at=workbook.updated_at,
        description=workbook.description or "",
        tags=sorted(workbook.tags) if workbook.tags else [],
        show_tabs=bool(workbook.show_tabs),
        size_bytes=int(workbook.size or 0),
        webpage_url=workbook.webpage_url or "",
    )


def compare_workbooks(
    source_wb: TSC.WorkbookItem,
    target_wb: TSC.WorkbookItem,
) -> List[WorkbookDiff]:
    """
    Compare top-level workbook metadata.
    Returns a list of WorkbookDiff records for every detected difference.
    """
    src = extract_workbook_meta(source_wb)
    tgt = extract_workbook_meta(target_wb)
    diffs: List[WorkbookDiff] = []

    def _check(field_name: str, sv: Any, tv: Any, severity: str = "WARNING") -> None:
        if sv != tv:
            diffs.append(WorkbookDiff(field=field_name, source_value=sv, target_value=tv, severity=severity))
            logger.warning("WB DIFF %-30s  src=%-40s  tgt=%s", field_name, sv, tv)

    _check("name",          src.name,          tgt.name,          "INFO")
    _check("project_name",  src.project_name,  tgt.project_name,  "WARNING")
    _check("project_id",    src.project_id,    tgt.project_id,    "WARNING")
    _check("owner_id",      src.owner_id,      tgt.owner_id,      "INFO")
    _check("show_tabs",     src.show_tabs,     tgt.show_tabs,     "WARNING")
    _check("description",   src.description,   tgt.description,   "INFO")

    # Tags
    src_tags = set(src.tags)
    tgt_tags = set(tgt.tags)
    if src_tags != tgt_tags:
        diffs.append(WorkbookDiff(
            field="tags",
            source_value=list(src_tags - tgt_tags) if src_tags - tgt_tags else [],
            target_value=list(tgt_tags - src_tags) if tgt_tags - src_tags else [],
            severity="INFO",
        ))

    # Date comparisons (informational)
    _check("created_at",  str(src.created_at), str(tgt.created_at), "INFO")
    _check("updated_at",  str(src.updated_at), str(tgt.updated_at), "INFO")

    # Size difference (flag if > 10 % delta — may indicate structural change)
    if src.size_bytes and tgt.size_bytes:
        delta_pct = abs(src.size_bytes - tgt.size_bytes) / max(src.size_bytes, 1) * 100
        if delta_pct > 10:
            diffs.append(WorkbookDiff(
                field="size_bytes",
                source_value=src.size_bytes,
                target_value=tgt.size_bytes,
                severity="INFO",
            ))

    if not diffs:
        logger.info("Workbook metadata: NO DIFFERENCES detected.")
    else:
        logger.info("Workbook metadata: %d difference(s) detected.", len(diffs))

    return diffs
