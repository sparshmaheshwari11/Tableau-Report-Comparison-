"""
modules/report_writer.py
-------------------------
Step 6 — Output.

Assembles all comparison results into:
  1. Console summary (rich tables)
  2. CSV report  (one file per diff category)
  3. JSON report (structured, machine-readable)
  4. Excel report (one tab per diff category)

All outputs land in the configured output_dir.
"""
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich import box

from modules.workbook_comparator import WorkbookDiff
from modules.datasource_comparator import DatasourceDiff
from modules.sheet_comparator import SheetDiff
from modules.value_comparator import ValueDiff
from modules.logger import setup_logger

logger = setup_logger(__name__)
console = Console()


@dataclass
class ValidationReport:
    run_timestamp: str
    source_workbook_id: str
    source_workbook_name: str
    target_workbook_id: str
    target_workbook_name: str
    workbook_diffs: List[WorkbookDiff]
    datasource_diffs: List[DatasourceDiff]
    sheet_diffs: List[SheetDiff]
    value_diffs: List[ValueDiff]

    @property
    def total_errors(self) -> int:
        return self._count_by_severity("ERROR")

    @property
    def total_warnings(self) -> int:
        return self._count_by_severity("WARNING")

    def _count_by_severity(self, sev: str) -> int:
        count = 0
        for d in (*self.workbook_diffs, *self.datasource_diffs, *self.sheet_diffs, *self.value_diffs):
            if getattr(d, "severity", "") == sev:
                count += 1
        return count


def write_report(report: ValidationReport, output_dir: str, config) -> None:
    """Write all configured report formats."""
    os.makedirs(output_dir, exist_ok=True)

    if config.console_summary:
        _print_console_summary(report)

    if config.csv_report:
        _write_csv(report, output_dir)

    if config.json_report:
        _write_json(report, output_dir)

    if config.excel_report:
        _write_excel(report, output_dir)


# ── Console output (rich) ─────────────────────────────────────────────────────

def _print_console_summary(report: ValidationReport) -> None:
    ts = report.run_timestamp

    console.rule(f"[bold blue]Tableau Validation Report — {ts}")
    console.print(
        f"\n[bold]Source:[/bold] {report.source_workbook_name}  ({report.source_workbook_id})"
    )
    console.print(
        f"[bold]Target:[/bold] {report.target_workbook_name}  ({report.target_workbook_id})\n"
    )

    _print_diff_table(
        title="Workbook Differences",
        rows=report.workbook_diffs,
        columns=["field", "source_value", "target_value", "severity"],
    )
    _print_diff_table(
        title="Datasource Differences",
        rows=report.datasource_diffs,
        columns=["diff_type", "datasource_name", "field", "source_value", "target_value", "severity"],
    )
    _print_diff_table(
        title="Sheet / Dashboard / Parameter Differences",
        rows=report.sheet_diffs,
        columns=["diff_type", "sheet_name", "field", "source_value", "target_value", "severity"],
    )
    _print_diff_table(
        title="Value / Data Differences",
        rows=report.value_diffs,
        columns=["sheet_name", "diff_type", "column", "detail", "source_value", "target_value", "severity"],
    )

    err_color  = "red"   if report.total_errors   else "green"
    warn_color = "yellow" if report.total_warnings else "green"
    console.print()
    console.print(
        f"[bold]Total ERRORs:[/bold]   [{err_color}]{report.total_errors}[/{err_color}]"
    )
    console.print(
        f"[bold]Total WARNINGs:[/bold] [{warn_color}]{report.total_warnings}[/{warn_color}]"
    )
    console.rule()


def _print_diff_table(title: str, rows: List[Any], columns: List[str]) -> None:
    tbl = Table(
        title=f"[bold]{title}[/bold]",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        row_styles=["", "dim"],
        expand=True,
    )
    for col in columns:
        tbl.add_column(col.replace("_", " ").title(), overflow="fold")

    for row in rows:
        d = asdict(row)
        sev = d.get("severity", "INFO")
        color = {"ERROR": "red", "WARNING": "yellow", "INFO": "white"}.get(sev, "white")
        tbl.add_row(*[f"[{color}]{str(d.get(c, ''))}[/{color}]" for c in columns])

    if not rows:
        tbl.add_row(*["✔ None" if i == 0 else "" for i in range(len(columns))])

    console.print(tbl)
    console.print()


# ── CSV output ────────────────────────────────────────────────────────────────

def _write_csv(report: ValidationReport, output_dir: str) -> None:
    _diffs_to_df(report.workbook_diffs).to_csv(
        os.path.join(output_dir, "workbook_diffs.csv"), index=False
    )
    _diffs_to_df(report.datasource_diffs).to_csv(
        os.path.join(output_dir, "datasource_diffs.csv"), index=False
    )
    _diffs_to_df(report.sheet_diffs).to_csv(
        os.path.join(output_dir, "sheet_diffs.csv"), index=False
    )
    _diffs_to_df(report.value_diffs).to_csv(
        os.path.join(output_dir, "value_diffs.csv"), index=False
    )
    logger.info("CSV reports written to: %s", output_dir)


# ── JSON output ───────────────────────────────────────────────────────────────

def _write_json(report: ValidationReport, output_dir: str) -> None:
    payload = {
        "run_timestamp":        report.run_timestamp,
        "source_workbook_id":   report.source_workbook_id,
        "source_workbook_name": report.source_workbook_name,
        "target_workbook_id":   report.target_workbook_id,
        "target_workbook_name": report.target_workbook_name,
        "summary": {
            "total_errors":   report.total_errors,
            "total_warnings": report.total_warnings,
            "workbook_diff_count":   len(report.workbook_diffs),
            "datasource_diff_count": len(report.datasource_diffs),
            "sheet_diff_count":      len(report.sheet_diffs),
            "value_diff_count":      len(report.value_diffs),
        },
        "workbook_diffs":   [_safe_asdict(d) for d in report.workbook_diffs],
        "datasource_diffs": [_safe_asdict(d) for d in report.datasource_diffs],
        "sheet_diffs":      [_safe_asdict(d) for d in report.sheet_diffs],
        "value_diffs":      [_safe_asdict(d) for d in report.value_diffs],
    }
    out_path = os.path.join(output_dir, "validation_report.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    logger.info("JSON report written to: %s", out_path)


# ── Excel output ──────────────────────────────────────────────────────────────

def _write_excel(report: ValidationReport, output_dir: str) -> None:
    out_path = os.path.join(output_dir, "validation_report.xlsx")
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        _write_excel_sheet(writer, "Summary",              _make_summary_df(report))
        _write_excel_sheet(writer, "Workbook Diffs",       _diffs_to_df(report.workbook_diffs))
        _write_excel_sheet(writer, "Datasource Diffs",     _diffs_to_df(report.datasource_diffs))
        _write_excel_sheet(writer, "Sheet Diffs",          _diffs_to_df(report.sheet_diffs))
        _write_excel_sheet(writer, "Value Diffs",          _diffs_to_df(report.value_diffs))
    logger.info("Excel report written to: %s", out_path)


def _write_excel_sheet(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame) -> None:
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]
    # Auto-fit columns
    for col in ws.columns:
        max_len = max((len(str(cell.value)) for cell in col if cell.value), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _diffs_to_df(diffs: List[Any]) -> pd.DataFrame:
    if not diffs:
        return pd.DataFrame()
    return pd.DataFrame([asdict(d) for d in diffs])


def _make_summary_df(report: ValidationReport) -> pd.DataFrame:
    rows = [
        {"Category": "Source Workbook",    "Value": f"{report.source_workbook_name} ({report.source_workbook_id})"},
        {"Category": "Target Workbook",    "Value": f"{report.target_workbook_name} ({report.target_workbook_id})"},
        {"Category": "Run Timestamp",      "Value": report.run_timestamp},
        {"Category": "Total ERRORs",       "Value": report.total_errors},
        {"Category": "Total WARNINGs",     "Value": report.total_warnings},
        {"Category": "Workbook Diffs",     "Value": len(report.workbook_diffs)},
        {"Category": "Datasource Diffs",   "Value": len(report.datasource_diffs)},
        {"Category": "Sheet Diffs",        "Value": len(report.sheet_diffs)},
        {"Category": "Value Diffs",        "Value": len(report.value_diffs)},
    ]
    return pd.DataFrame(rows)


def _safe_asdict(obj: Any) -> Dict:
    d = asdict(obj)
    # Ensure all values are JSON-serialisable
    return {k: str(v) if not isinstance(v, (int, float, bool, type(None), str, list, dict)) else v
            for k, v in d.items()}
