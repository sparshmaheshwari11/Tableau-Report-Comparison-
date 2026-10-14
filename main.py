"""
main.py
-------
Tableau Workbook Validation Framework
Enterprise-grade end-to-end comparison of two Tableau workbooks.

Usage:
    python main.py --config config/config.json
    python main.py --config config/config.json --source-url <url> --target-url <url>
    python main.py --config config/config.json --skip-values
    python main.py --config config/config.json --log-level DEBUG

Output folder per run:
    output/<TargetWorkbookName>_<YYYYMMDD_HHMMSS>/
        workbook_diffs.csv
        datasource_diffs.csv
        sheet_diffs.csv
        value_diffs.csv
        validation_report.json
        validation_report.xlsx
        validation.log
        screenshots/
            <view>_source.png
            <view>_target.png
            <view>_DIFF.png

Architecture:
    Step 1  -> Workbook metadata  (workbook_comparator)
    Step 2  -> Datasource schema  (datasource_comparator)
    Step 3  -> Sheets/Visuals     (sheet_comparator)
    Step 4  -> Screenshot diff    (value_comparator)
    Step 5  -> Dashboards         (sheet_comparator)
    Step 6  -> Output             (report_writer)
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone

from modules.config_loader import load_config
from modules.logger import setup_logger
from modules.tableau_client import TableauClient
from modules.workbook_comparator import compare_workbooks
from modules.datasource_comparator import (
    fetch_datasource_metadata,
    compare_datasources,
)
from modules.sheet_comparator import (
    fetch_sheet_metadata,
    compare_sheets,
)
from modules.value_comparator import compare_view_data
from modules.report_writer import ValidationReport, write_report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tableau Workbook Validation Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--config",
        default="config/config.json",
        help="Path to config.json (default: config/config.json)",
    )
    p.add_argument(
        "--source-url",
        help="Override source workbook URL from config",
    )
    p.add_argument(
        "--target-url",
        help="Override target workbook URL from config",
    )
    p.add_argument(
        "--skip-values",
        action="store_true",
        help="Skip screenshot / value comparison (Step 4)",
    )
    p.add_argument(
        "--skip-metadata",
        action="store_true",
        help="Skip workbook / datasource / sheet metadata comparison",
    )
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log level from config",
    )
    p.add_argument(
        "--output-dir",
        default=None,
        help="Override base output directory from config",
    )
    return p.parse_args()


def _make_run_output_dir(base_dir: str, target_wb_name: str, run_ts_local: str) -> str:
    """
    Build a unique output folder per run:
        <base_dir>/<TargetWorkbookName>_<YYYYMMDD_HHMMSS>/

    Example:
        output/AI_TUTOR_DASHBOARD_20260304_154512/
    """
    # Sanitise workbook name — keep alphanumeric, spaces -> underscore
    safe_name = re.sub(r"[^\w\s-]", "", target_wb_name).strip()
    safe_name = re.sub(r"[\s]+", "_", safe_name)[:50]  # cap at 50 chars

    # Timestamp in local time, filesystem-safe
    ts = run_ts_local  # already formatted as YYYYMMDD_HHMMSS by caller

    folder_name = f"{safe_name}_{ts}"
    run_dir = os.path.join(base_dir, folder_name)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def main() -> int:
    args = parse_args()

    # ── Load config ───────────────────────────────────────────────────────────
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[FATAL] Configuration error: {exc}", file=sys.stderr)
        return 2

    # Apply CLI overrides
    if args.source_url:
        config.comparison.source_workbook_url = args.source_url
    if args.target_url:
        config.comparison.target_workbook_url = args.target_url
    if args.log_level:
        config.output.log_level = args.log_level
    if args.output_dir:
        config.output.output_dir = args.output_dir
    if args.skip_values:
        config.comparison.value_comparison_enabled = False

    # ── Timestamps ────────────────────────────────────────────────────────────
    now = datetime.now()
    run_ts_utc   = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_ts_local = now.strftime("%Y%m%d_%H%M%S")   # used for folder name

    # ── Temporary logger before we know the workbook name ────────────────────
    # (full logger with file handler is set up once we have the run_dir)
    bootstrap_logger = setup_logger("main", log_level=config.output.log_level)
    bootstrap_logger.info("=" * 72)
    bootstrap_logger.info("Tableau Validation Framework — run started at %s", run_ts_utc)
    bootstrap_logger.info("Source URL: %s", config.comparison.source_workbook_url)
    bootstrap_logger.info("Target URL: %s", config.comparison.target_workbook_url)
    bootstrap_logger.info("=" * 72)

    t_start = time.perf_counter()
    client = TableauClient(config)

    try:
        # ── Connect ───────────────────────────────────────────────────────────
        client.connect()

        # ── Resolve workbook IDs from URLs ────────────────────────────────────
        bootstrap_logger.info("Resolving workbook IDs from URLs ...")
        src_id = client.resolve_workbook_id_from_url(config.comparison.source_workbook_url)
        tgt_id = client.resolve_workbook_id_from_url(config.comparison.target_workbook_url)
        bootstrap_logger.info("Source workbook LUID: %s", src_id)
        bootstrap_logger.info("Target workbook LUID: %s", tgt_id)

        # ── Fetch top-level workbook items via TSC ────────────────────────────
        src_wb = client.get_workbook_by_id(src_id)
        tgt_wb = client.get_workbook_by_id(tgt_id)

        src_wb_name = src_wb.name or src_id
        tgt_wb_name = tgt_wb.name or tgt_id

        # ── Build unique run output directory ─────────────────────────────────
        # Named after TARGET workbook + timestamp so every run is distinct
        run_dir = _make_run_output_dir(
            base_dir=config.output.output_dir,
            target_wb_name=tgt_wb_name,
            run_ts_local=run_ts_local,
        )
        bootstrap_logger.info("Run output directory: %s", run_dir)

        # ── Re-initialise logger now we have the run directory ────────────────
        log_file = os.path.join(run_dir, "validation.log")
        logger = setup_logger(
            "main",
            log_level=config.output.log_level,
            log_file=log_file,
        )
        logger.info("=" * 72)
        logger.info("Source workbook : %s  (%s)", src_wb_name, src_id)
        logger.info("Target workbook : %s  (%s)", tgt_wb_name, tgt_id)
        logger.info("Output directory: %s", run_dir)
        logger.info("=" * 72)

        # ── STEP 1 — Workbook metadata ────────────────────────────────────────
        workbook_diffs = []
        if not args.skip_metadata:
            logger.info("-" * 60)
            logger.info("STEP 1: Workbook metadata comparison")
            workbook_diffs = compare_workbooks(src_wb, tgt_wb)

        # ── Populate connections via TSC ──────────────────────────────────────
        src_connections = client.get_workbook_connections(src_wb)
        tgt_connections = client.get_workbook_connections(tgt_wb)

        # ── STEP 2 — Datasource validation ───────────────────────────────────
        datasource_diffs = []
        if not args.skip_metadata:
            logger.info("-" * 60)
            logger.info("STEP 2: Datasource comparison")
            src_datasources = fetch_datasource_metadata(client, src_id, src_connections)
            tgt_datasources = fetch_datasource_metadata(client, tgt_id, tgt_connections)
            datasource_diffs = compare_datasources(src_datasources, tgt_datasources)

        # ── STEP 3 & 5 — Sheets / Dashboards / Parameters ────────────────────
        sheet_diffs = []
        if not args.skip_metadata:
            logger.info("-" * 60)
            logger.info("STEP 3 & 5: Sheet / Dashboard / Parameter comparison")
            src_sheet_meta = fetch_sheet_metadata(client, src_id)
            tgt_sheet_meta = fetch_sheet_metadata(client, tgt_id)
            sheet_diffs = compare_sheets(src_sheet_meta, tgt_sheet_meta)

        # ── STEP 4 — Screenshot comparison ───────────────────────────────────
        value_diffs = []
        if config.comparison.value_comparison_enabled:
            logger.info("-" * 60)
            logger.info("STEP 4: Screenshot / visual comparison")
            src_views = client.get_workbook_views(src_wb)
            tgt_views = client.get_workbook_views(tgt_wb)
            value_diffs = compare_view_data(
                client,
                src_views,
                tgt_views,
                config.comparison,
                output_dir=run_dir,        # screenshots go inside the run folder
            )

        # ── Assemble report ───────────────────────────────────────────────────
        report = ValidationReport(
            run_timestamp=run_ts_utc,
            source_workbook_id=src_id,
            source_workbook_name=src_wb_name,
            target_workbook_id=tgt_id,
            target_workbook_name=tgt_wb_name,
            workbook_diffs=workbook_diffs,
            datasource_diffs=datasource_diffs,
            sheet_diffs=sheet_diffs,
            value_diffs=value_diffs,
        )

        # ── STEP 6 — Output ───────────────────────────────────────────────────
        logger.info("-" * 60)
        logger.info("STEP 6: Writing reports to '%s'", run_dir)

        # Update output config to point at the run directory
        config.output.output_dir = run_dir
        config.output.log_file   = log_file
        write_report(report, run_dir, config.output)

        elapsed = time.perf_counter() - t_start
        logger.info("=" * 72)
        logger.info(
            "Validation complete in %.1f s — ERRORs=%d  WARNINGs=%d",
            elapsed,
            report.total_errors,
            report.total_warnings,
        )
        logger.info("Output saved to: %s", run_dir)
        logger.info("=" * 72)

        # Print final path to console so user knows exactly where to look
        print(f"\n{'=' * 60}")
        print(f"  RUN COMPLETE")
        print(f"  Output folder : {os.path.abspath(run_dir)}")
        print(f"  Screenshots   : {os.path.abspath(os.path.join(run_dir, 'screenshots'))}")
        print(f"  ERRORs        : {report.total_errors}")
        print(f"  WARNINGs      : {report.total_warnings}")
        print(f"{'=' * 60}\n")

        return 1 if report.total_errors else 0

    except KeyboardInterrupt:
        bootstrap_logger.warning("Interrupted by user.")
        return 130

    except Exception as exc:
        bootstrap_logger.exception("Unhandled exception: %s", exc)
        return 1

    finally:
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())