from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any

import tableauserverclient as TSC
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

from modules.config_loader import (
    AppConfig, TableauConfig, ComparisonConfig, OutputConfig, PerformanceConfig
)
from modules.tableau_client import TableauClient

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "output" / "web-runs"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Tableau Workbook Validator",
    description="Compare two Tableau Server/Cloud workbooks using the existing validation engine.",
    version="2.0.0",
)

class AuthRequest(BaseModel):
    server_url: str = Field(min_length=8, max_length=500)
    site_id: str = Field(default="", max_length=200)
    pat_name: str = Field(min_length=1, max_length=300)
    pat_secret: str = Field(min_length=1, max_length=2000)
    api_version: str = Field(default="3.19", max_length=20)

class CompareRequest(AuthRequest):
    source_url: str = Field(min_length=8, max_length=2000)
    target_url: str = Field(min_length=8, max_length=2000)
    numeric_tolerance: float = Field(default=0.001, ge=0, le=1)
    compare_values: bool = True

def _validate_http_url(value: str, label: str) -> str:
    value = value.strip()
    if not value.startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail=f"{label} must start with http:// or https://.")
    return value.rstrip("/")

def _config_from_request(data: AuthRequest, output_dir: str = "./output") -> AppConfig:
    return AppConfig(
        tableau=TableauConfig(
            server_url=_validate_http_url(data.server_url, "Tableau Server URL"),
            site_id=data.site_id.strip(),
            pat_name=data.pat_name,
            pat_value=data.pat_secret,
            api_version=data.api_version.strip() or "3.19",
        ),
        comparison=ComparisonConfig(
            source_workbook_url="",
            target_workbook_url="",
            value_comparison_enabled=True,
            numeric_tolerance=0.001,
            string_case_sensitive=False,
            pagination_page_size=1000,
        ),
        output=OutputConfig(
            output_dir=output_dir,
            csv_report=True,
            json_report=True,
            excel_report=True,
            console_summary=False,
            log_level="INFO",
            log_file=str(Path(output_dir) / "validation.log"),
        ),
        performance=PerformanceConfig(),
    )

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "application": "Tableau Workbook Validator"}

@app.post("/api/test-connection")
def test_connection(data: AuthRequest) -> dict[str, Any]:
    """Test credentials without logging or persisting the PAT secret."""
    config = _config_from_request(data)
    client = TableauClient(config)
    try:
        client.connect()
        server_version = getattr(client.server, "version", None)
        site_id = getattr(client.server, "site_id", None)
        return {
            "ok": True,
            "message": "Authentication successful. Enter the two dashboard URLs to continue.",
            "site_id": site_id or "",
            "api_version": config.tableau.api_version,
            "server_url": config.tableau.server_url,
        }
    except Exception as exc:
        # Do not echo credentials or raw request bodies.
        raise HTTPException(status_code=401, detail=f"Could not authenticate to Tableau. Check the server URL, site ID, PAT name, PAT secret, and API version. Details: {str(exc)[:500]}")
    finally:
        client.disconnect()

@app.post("/api/compare")
def compare(data: CompareRequest) -> dict[str, Any]:
    """Run the repository's existing comparison engine with per-request credentials."""
    server_url = _validate_http_url(data.server_url, "Tableau Server URL")
    source_url = _validate_http_url(data.source_url, "Original dashboard URL")
    target_url = _validate_http_url(data.target_url, "Updated dashboard URL")

    run_id = uuid.uuid4().hex
    run_base = OUTPUT_ROOT / run_id
    run_base.mkdir(parents=True, exist_ok=True)

    # The existing CLI and comparison modules remain the engine of record.
    config_payload = {
        "tableau": {
            "server_url": server_url,
            "site_id": data.site_id.strip(),
            "pat_name": data.pat_name,
            "pat_value": data.pat_secret,
            "api_version": data.api_version.strip() or "3.19",
        },
        "comparison": {
            "source_workbook_url": source_url,
            "target_workbook_url": target_url,
            "value_comparison_enabled": data.compare_values,
            "value_comparison_max_rows": 500000,
            "numeric_tolerance": data.numeric_tolerance,
            "string_case_sensitive": False,
            "pagination_page_size": 1000,
        },
        "output": {
            "output_dir": str(run_base),
            "csv_report": True,
            "json_report": True,
            "excel_report": True,
            "console_summary": False,
            "log_level": "INFO",
            "log_file": str(run_base / "validation.log"),
        },
        "performance": {
            "request_timeout_seconds": 120,
            "max_retries": 3,
            "retry_backoff_seconds": 5,
            "parallel_sheet_comparison": False,
            "max_workers": 4,
        },
    }
    config_path = run_base / "request-config.json"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    env = os.environ.copy()
    # Ensure the credentials entered in the form take precedence over stale machine env vars.
    env.update({
        "TABLEAU_SERVER_URL": server_url,
        "TABLEAU_SITE_ID": data.site_id.strip(),
        "TABLEAU_PAT_NAME": data.pat_name,
        "TABLEAU_PAT_VALUE": data.pat_secret,
        "TABLEAU_API_VERSION": data.api_version.strip() or "3.19",
    })
    command = [
        sys.executable, str(ROOT / "main.py"),
        "--config", str(config_path),
        "--source-url", source_url,
        "--target-url", target_url,
        "--output-dir", str(run_base),
    ]
    try:
        proc = subprocess.run(
            command, cwd=str(ROOT), env=env, capture_output=True, text=True,
            timeout=1800, check=False
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(run_base, ignore_errors=True)
        raise HTTPException(status_code=504, detail="Comparison timed out after 30 minutes. Try smaller workbooks or review server connectivity.")
    finally:
        # Never retain PAT secrets in generated config files.
        try:
            config_path.unlink(missing_ok=True)
        except Exception:
            pass

    report_path = None
    for candidate in run_base.rglob("validation_report.json"):
        report_path = candidate
        break

    if report_path is None:
        tail = (proc.stderr or proc.stdout or "No diagnostic output was produced.")[-1800:]
        # Keep failed runs available for diagnostics, but never include the secret.
        raise HTTPException(
            status_code=502,
            detail="The comparison engine did not produce a report. Check workbook URLs, permissions, Metadata API availability, and server logs. " + tail
        )

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read the generated report: {exc}")

    zip_path = run_base / "comparison-report.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in report_path.parent.rglob("*"):
            if path.is_file() and path != zip_path:
                zf.write(path, arcname=path.relative_to(report_path.parent))

    summary = report.get("summary", {})
    return {
        "ok": True,
        "run_id": run_id,
        "summary": summary,
        "source_workbook_name": report.get("source_workbook_name", "Original workbook"),
        "target_workbook_name": report.get("target_workbook_name", "Updated workbook"),
        "workbook_diffs": report.get("workbook_diffs", []),
        "datasource_diffs": report.get("datasource_diffs", []),
        "sheet_diffs": report.get("sheet_diffs", []),
        "value_diffs": report.get("value_diffs", []),
        "download_url": f"/api/download/{run_id}",
        "exit_code": proc.returncode,
    }

@app.get("/api/download/{run_id}")
def download_report(run_id: str):
    if not run_id.isalnum() or len(run_id) != 32:
        raise HTTPException(status_code=404, detail="Report not found.")
    zip_path = OUTPUT_ROOT / run_id / "comparison-report.zip"
    if not zip_path.exists():
        # Report ZIP is created at the run base; locate it in the run folder.
        matches = list((OUTPUT_ROOT / run_id).rglob("comparison-report.zip"))
        zip_path = matches[0] if matches else zip_path
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Report download is no longer available.")
    return FileResponse(zip_path, filename="tableau-comparison-report.zip", media_type="application/zip")

app.mount("/", StaticFiles(directory=str(ROOT / "static"), html=True), name="static")
