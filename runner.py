"""
runner.py
---------
Thin wrapper that accepts source_url + target_url,
runs the full validation pipeline, and yields status dicts
as a generator for real-time streaming to the Streamlit UI.

No existing module files are changed.
"""
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from typing import Generator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.config_loader import load_config
from modules.tableau_client import TableauClient
from modules.workbook_comparator import compare_workbooks
from modules.datasource_comparator import fetch_datasource_metadata, compare_datasources
from modules.sheet_comparator import fetch_sheet_metadata, compare_sheets
from modules.value_comparator import compare_view_data
from modules.report_writer import ValidationReport, write_report


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    safe = re.sub(r"[^\w\s-]", "", name).strip()
    return re.sub(r"\s+", "_", safe)[:50]


def _make_run_dir(base_dir: str, tgt_name: str, ts: str) -> str:
    folder = f"{_safe_name(tgt_name)}_{ts}"
    path = os.path.join(base_dir, folder)
    os.makedirs(path, exist_ok=True)
    return path


def _zip_run_dir(run_dir: str) -> str:
    """Zip the entire run output folder. Returns zip file path."""
    zip_path = run_dir + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(run_dir):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, os.path.dirname(run_dir))
                zf.write(full_path, arcname)
    return zip_path


def _msg(step: str, status: str, message: str, **extra) -> dict:
    return {"step": step, "status": status, "message": message, **extra}


# ── Main generator ────────────────────────────────────────────────────────────

def run_validation(
    source_url: str,
    target_url: str,
    config_path: str = "config/config.json",
    base_output_dir: str = "./output",
    skip_screenshots: bool = False,
    skip_metadata: bool = False,
) -> Generator[dict, None, None]:
    """
    Generator — yields status dicts at each pipeline step.

    Possible dict shapes:
      { "step": str, "status": "running"|"ok"|"error"|"warn", "message": str }

    Final yield on success:
      { "step": "done", "status": "ok", "run_dir": str, "zip_path": str,
        "report": ValidationReport, "screenshot_paths": list }
    """

    # ── Load config ───────────────────────────────────────────────────────────
    try:
        config = load_config(config_path)
        config.comparison.source_workbook_url = source_url
        config.comparison.target_workbook_url = target_url
        if skip_screenshots:
            config.comparison.value_comparison_enabled = False
        yield _msg("config", "ok", "Configuration loaded successfully.")
    except Exception as exc:
        yield _msg("config", "error", f"Config error: {exc}")
        return

    now          = datetime.now()
    run_ts_utc   = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_ts_local = now.strftime("%Y%m%d_%H%M%S")

    client = TableauClient(config)

    try:
        # ── Authenticate ──────────────────────────────────────────────────────
        yield _msg("connect", "running", "Connecting to Tableau Server…")
        client.connect()
        yield _msg("connect", "ok", f"Authenticated — {config.tableau.server_url}")

        # ── Resolve workbook IDs ──────────────────────────────────────────────
        yield _msg("resolve", "running", "Resolving workbook IDs from URLs…")
        src_id = client.resolve_workbook_id_from_url(source_url)
        tgt_id = client.resolve_workbook_id_from_url(target_url)

        src_wb = client.get_workbook_by_id(src_id)
        tgt_wb = client.get_workbook_by_id(tgt_id)
        src_name = src_wb.name or src_id
        tgt_name = tgt_wb.name or tgt_id
        yield _msg("resolve", "ok",
                   f"Source: \"{src_name}\"  →  Target: \"{tgt_name}\"",
                   src_name=src_name, tgt_name=tgt_name)

        # ── Create timestamped run directory ──────────────────────────────────
        run_dir  = _make_run_dir(base_output_dir, tgt_name, run_ts_local)
        log_file = os.path.join(run_dir, "validation.log")
        config.output.output_dir = run_dir
        config.output.log_file   = log_file
        yield _msg("resolve", "ok", f"Output folder: {os.path.basename(run_dir)}")

        # ── Step 1 — Workbook metadata ────────────────────────────────────────
        workbook_diffs = []
        if not skip_metadata:
            yield _msg("step1", "running", "Comparing workbook metadata…")
            workbook_diffs = compare_workbooks(src_wb, tgt_wb)
            n = len(workbook_diffs)
            yield _msg("step1",
                       "ok" if n == 0 else "warn",
                       f"{n} difference(s) found.",
                       count=n)

        # ── Step 2 — Datasources ──────────────────────────────────────────────
        datasource_diffs = []
        if not skip_metadata:
            yield _msg("step2", "running", "Comparing datasources…")
            src_conns = client.get_workbook_connections(src_wb)
            tgt_conns = client.get_workbook_connections(tgt_wb)
            src_ds    = fetch_datasource_metadata(client, src_id, src_conns)
            tgt_ds    = fetch_datasource_metadata(client, tgt_id, tgt_conns)
            datasource_diffs = compare_datasources(src_ds, tgt_ds)
            n = len(datasource_diffs)
            errs = sum(1 for d in datasource_diffs if d.severity == "ERROR")
            yield _msg("step2",
                       "ok" if errs == 0 else "error",
                       f"{n} difference(s) found.",
                       count=n)

        # ── Step 3 — Sheets & Dashboards ─────────────────────────────────────
        sheet_diffs = []
        if not skip_metadata:
            yield _msg("step3", "running", "Comparing sheets & dashboards…")
            src_sheets = fetch_sheet_metadata(client, src_id)
            tgt_sheets = fetch_sheet_metadata(client, tgt_id)
            sheet_diffs = compare_sheets(src_sheets, tgt_sheets)
            n = len(sheet_diffs)
            errs = sum(1 for d in sheet_diffs if d.severity == "ERROR")
            yield _msg("step3",
                       "ok" if errs == 0 else "error",
                       f"{n} difference(s) found.",
                       count=n)

        # ── Step 4 — Screenshots ──────────────────────────────────────────────
        value_diffs      = []
        screenshot_paths = []
        if config.comparison.value_comparison_enabled:
            yield _msg("step4", "running", "Downloading & comparing screenshots…")
            src_views = client.get_workbook_views(src_wb)
            tgt_views = client.get_workbook_views(tgt_wb)
            value_diffs = compare_view_data(
                client, src_views, tgt_views,
                config.comparison, output_dir=run_dir,
            )
            sc_dir = os.path.join(run_dir, "screenshots")
            if os.path.exists(sc_dir):
                screenshot_paths = sorted([
                    os.path.join(sc_dir, f)
                    for f in os.listdir(sc_dir)
                    if f.endswith("_DIFF.png")
                ])
            n    = len(value_diffs)
            errs = sum(1 for d in value_diffs if d.severity == "ERROR")
            yield _msg("step4",
                       "ok" if errs == 0 else "error",
                       f"{n} visual difference(s) found.",
                       count=n)
        else:
            yield _msg("step4", "warn", "Screenshot comparison skipped.")

        # ── Step 5 — Write reports ────────────────────────────────────────────
        yield _msg("step5", "running", "Writing report files…")
        report = ValidationReport(
            run_timestamp        = run_ts_utc,
            source_workbook_id   = src_id,
            source_workbook_name = src_name,
            target_workbook_id   = tgt_id,
            target_workbook_name = tgt_name,
            workbook_diffs       = workbook_diffs,
            datasource_diffs     = datasource_diffs,
            sheet_diffs          = sheet_diffs,
            value_diffs          = value_diffs,
        )
        write_report(report, run_dir, config.output)
        yield _msg("step5", "ok", "CSV, JSON, Excel & log written.")

        # ── Zip ───────────────────────────────────────────────────────────────
        yield _msg("zip", "running", "Packaging results into ZIP…")
        zip_path = _zip_run_dir(run_dir)
        yield _msg("zip", "ok", f"ZIP ready — {os.path.basename(zip_path)}")

        # ── Done ──────────────────────────────────────────────────────────────
        yield _msg(
            "done", "ok",
            f"Validation complete — {report.total_errors} error(s), "
            f"{report.total_warnings} warning(s).",
            run_dir          = run_dir,
            zip_path         = zip_path,
            report           = report,
            screenshot_paths = screenshot_paths,
        )

    except Exception as exc:
        yield _msg("error", "error", f"Unexpected error: {exc}")

    finally:
        client.disconnect()