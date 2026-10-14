"""
modules/config_loader.py
------------------------
Load, validate and expose the JSON configuration file.

Credential resolution priority (highest to lowest):
  1. Streamlit secrets      — st.secrets  (when deployed on Streamlit Cloud)
  2. Environment variables  — os.environ  (CI/CD or local .env)
  3. config.json file       — fallback    (local development)
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from modules.logger import setup_logger

logger = setup_logger(__name__)


def _get_secret(key: str, fallback: str = "") -> str:
    """Read an environment override, falling back to the supplied config value.

    The web app passes credentials per request and sets environment overrides only
    for the lifetime of the comparison subprocess. No Streamlit dependency remains.
    """
    return os.environ.get(key) or fallback


@dataclass
class TableauConfig:
    server_url:  str
    site_id:     str
    pat_name:    str
    pat_value:   str
    api_version: str = "3.19"


@dataclass
class ComparisonConfig:
    source_workbook_url:      str
    target_workbook_url:      str
    value_comparison_enabled: bool  = True
    value_comparison_max_rows: int  = 500_000
    numeric_tolerance:        float = 0.001
    string_case_sensitive:    bool  = False
    pagination_page_size:     int   = 1000


@dataclass
class OutputConfig:
    output_dir:      str  = "./output"
    csv_report:      bool = True
    json_report:     bool = True
    excel_report:    bool = True
    console_summary: bool = True
    log_level:       str  = "INFO"
    log_file:        str  = "./output/validation.log"


@dataclass
class PerformanceConfig:
    request_timeout_seconds:    int  = 120
    max_retries:                int  = 3
    retry_backoff_seconds:      int  = 5
    parallel_sheet_comparison:  bool = False
    max_workers:                int  = 4


@dataclass
class AppConfig:
    tableau:     TableauConfig
    comparison:  ComparisonConfig
    output:      OutputConfig
    performance: PerformanceConfig


def load_config(config_path: str = "config/config.json") -> AppConfig:
    """
    Load configuration. Credentials are resolved from Streamlit secrets
    or environment variables first, falling back to config.json values.
    This means config.json does NOT need real credentials when deployed.
    """
    path = Path(config_path)

    # config.json is optional on Streamlit Cloud (secrets cover credentials)
    # but must exist locally with at least the non-secret settings
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        logger.warning(
            "config.json not found at '%s' — relying entirely on secrets/env vars.",
            config_path,
        )
        raw = {}

    # ── Tableau credentials ───────────────────────────────────────────────────
    t = raw.get("tableau", {})
    tableau_cfg = TableauConfig(
        server_url  = _get_secret("TABLEAU_SERVER_URL",  t.get("server_url",  "")),
        site_id     = _get_secret("TABLEAU_SITE_ID",     t.get("site_id",     "")),
        pat_name    = _get_secret("TABLEAU_PAT_NAME",    t.get("pat_name",    "")),
        pat_value   = _get_secret("TABLEAU_PAT_VALUE",   t.get("pat_value",   "")),
        api_version = _get_secret("TABLEAU_API_VERSION", t.get("api_version", "3.19")),
    )
    _validate_tableau_config(tableau_cfg)

    # ── Comparison settings ───────────────────────────────────────────────────
    c = raw.get("comparison", {})
    comparison_cfg = ComparisonConfig(
        source_workbook_url       = c.get("source_workbook_url",       ""),
        target_workbook_url       = c.get("target_workbook_url",       ""),
        value_comparison_enabled  = c.get("value_comparison_enabled",  True),
        value_comparison_max_rows = c.get("value_comparison_max_rows", 500_000),
        numeric_tolerance         = c.get("numeric_tolerance",         0.001),
        string_case_sensitive     = c.get("string_case_sensitive",     False),
        pagination_page_size      = c.get("pagination_page_size",      1000),
    )

    # ── Output settings ───────────────────────────────────────────────────────
    o = raw.get("output", {})
    output_cfg = OutputConfig(
        output_dir      = o.get("output_dir",      "./output"),
        csv_report      = o.get("csv_report",      True),
        json_report     = o.get("json_report",     True),
        excel_report    = o.get("excel_report",    True),
        console_summary = o.get("console_summary", True),
        log_level       = o.get("log_level",       "INFO"),
        log_file        = o.get("log_file",        "./output/validation.log"),
    )

    # ── Performance settings ──────────────────────────────────────────────────
    p = raw.get("performance", {})
    perf_cfg = PerformanceConfig(
        request_timeout_seconds   = p.get("request_timeout_seconds",   120),
        max_retries               = p.get("max_retries",               3),
        retry_backoff_seconds     = p.get("retry_backoff_seconds",     5),
        parallel_sheet_comparison = p.get("parallel_sheet_comparison", False),
        max_workers               = p.get("max_workers",               4),
    )

    os.makedirs(output_cfg.output_dir, exist_ok=True)
    logger.info("Configuration loaded from %s", config_path)

    return AppConfig(
        tableau     = tableau_cfg,
        comparison  = comparison_cfg,
        output      = output_cfg,
        performance = perf_cfg,
    )


def _validate_tableau_config(cfg: TableauConfig) -> None:
    missing = [
        name for name, val in [
            ("server_url", cfg.server_url),
            ("pat_name",   cfg.pat_name),
            ("pat_value",  cfg.pat_value),
        ]
        if not val
    ]
    if missing:
        raise ValueError(
            f"Missing required Tableau credentials: {missing}. "
            "Add them to Streamlit secrets, environment variables, or config.json."
        )