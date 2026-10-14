"""
modules/sheet_comparator.py
----------------------------
Steps 3 & 5 — Sheet / Visual and Dashboard comparison.

Uses Tableau Metadata API (GraphQL) to retrieve:
  - All sheets in each workbook
  - Fields used per sheet
  - Filters, parameters, calculations
  - Dashboard composition and layout
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from modules.tableau_client import TableauClient
from modules.logger import setup_logger

logger = setup_logger(__name__)


# ── GraphQL Query ─────────────────────────────────────────────────────────────

WORKBOOK_SHEETS_QUERY = """
query WorkbookSheets($workbookId: String!) {
  workbooksConnection(filter: { luid: $workbookId }) {
    nodes {
      luid
      name
      sheetsConnection {
        nodes {
          luid
          name
          sheetType
          createdAt
          updatedAt
          path
          index
          containedInDashboardsConnection {
            nodes {
              name
              luid
            }
          }
          fieldsConnection {
            nodes {
              id
              name
              dataType
              isHidden
              ... on CalculatedField {
                formula
              }
            }
          }
          filtersConnection {
            nodes {
              id
              field {
                name
                dataType
              }
            }
          }
        }
      }
      dashboardsConnection {
        nodes {
          luid
          name
          createdAt
          updatedAt
          path
          sheetsConnection {
            nodes {
              name
              luid
            }
          }
        }
      }
      parametersConnection {
        nodes {
          id
          name
          dataType
          defaultValue: defaultFormattedValue
          allowableValuesType
        }
      }
    }
  }
}
"""


@dataclass
class FilterInfo:
    id: str
    field_name: str
    field_data_type: str


@dataclass
class SheetInfo:
    luid: str
    name: str
    sheet_type: str         # worksheet | dashboard | story
    path: str
    index: int
    fields: List[Dict]      # [{id, name, dataType, formula?}]
    filters: List[FilterInfo]
    dashboards: List[str]   # names of dashboards containing this sheet
    created_at: Optional[str]
    updated_at: Optional[str]


@dataclass
class DashboardInfo:
    luid: str
    name: str
    path: str
    sheets: List[str]       # names of sheets on this dashboard
    created_at: Optional[str]
    updated_at: Optional[str]


@dataclass
class ParameterInfo:
    id: str
    name: str
    data_type: str
    default_value: str
    allowable_values_type: str


@dataclass
class SheetDiff:
    diff_type: str
    sheet_name: str
    field: str
    source_value: Any
    target_value: Any
    severity: str


def fetch_sheet_metadata(
    client: TableauClient,
    workbook_id: str,
) -> Dict[str, Any]:
    """
    Return a dict:
      {
        'sheets':     [SheetInfo, ...],
        'dashboards': [DashboardInfo, ...],
        'parameters': [ParameterInfo, ...],
      }
    """
    logger.info("Fetching sheet/dashboard metadata for workbook: %s", workbook_id)
    data = client.metadata_query(
        WORKBOOK_SHEETS_QUERY,
        variables={"workbookId": workbook_id},
    )
    nodes = data.get("workbooksConnection", {}).get("nodes", [])
    if not nodes:
        logger.warning("No sheet data returned for workbook: %s", workbook_id)
        return {"sheets": [], "dashboards": [], "parameters": []}

    wb = nodes[0]

    # Sheets
    sheets: List[SheetInfo] = []
    for s in wb.get("sheetsConnection", {}).get("nodes", []):
        filters = [
            FilterInfo(
                id=f.get("id", ""),
                field_name=(f.get("field") or {}).get("name", ""),
                field_data_type=(f.get("field") or {}).get("dataType", ""),
            )
            for f in s.get("filtersConnection", {}).get("nodes", [])
        ]
        sheets.append(SheetInfo(
            luid=s.get("luid", ""),
            name=s.get("name", ""),
            sheet_type=s.get("sheetType", ""),
            path=s.get("path", ""),
            index=int(s.get("index") or 0),
            fields=s.get("fieldsConnection", {}).get("nodes", []),
            filters=filters,
            dashboards=[
                d["name"]
                for d in s.get("containedInDashboardsConnection", {}).get("nodes", [])
            ],
            created_at=s.get("createdAt"),
            updated_at=s.get("updatedAt"),
        ))

    # Dashboards
    dashboards: List[DashboardInfo] = []
    for d in wb.get("dashboardsConnection", {}).get("nodes", []):
        dashboards.append(DashboardInfo(
            luid=d.get("luid", ""),
            name=d.get("name", ""),
            path=d.get("path", ""),
            sheets=[
                sh["name"]
                for sh in d.get("sheetsConnection", {}).get("nodes", [])
            ],
            created_at=d.get("createdAt"),
            updated_at=d.get("updatedAt"),
        ))

    # Parameters
    parameters: List[ParameterInfo] = []
    for p in wb.get("parametersConnection", {}).get("nodes", []):
        parameters.append(ParameterInfo(
            id=p.get("id", ""),
            name=p.get("name", ""),
            data_type=p.get("dataType", ""),
            default_value=p.get("defaultValue", ""),
            allowable_values_type=p.get("allowableValuesType", ""),
        ))

    logger.info(
        "WB %s — %d sheets, %d dashboards, %d parameters",
        workbook_id, len(sheets), len(dashboards), len(parameters),
    )
    return {"sheets": sheets, "dashboards": dashboards, "parameters": parameters}


def compare_sheets(
    src_meta: Dict[str, Any],
    tgt_meta: Dict[str, Any],
) -> List[SheetDiff]:
    """Compare sheets, dashboards, and parameters between source and target."""
    diffs: List[SheetDiff] = []

    diffs.extend(_compare_sheet_list(src_meta["sheets"], tgt_meta["sheets"]))
    diffs.extend(_compare_dashboards(src_meta["dashboards"], tgt_meta["dashboards"]))
    diffs.extend(_compare_parameters(src_meta["parameters"], tgt_meta["parameters"]))

    logger.info("Sheet comparison complete — %d difference(s).", len(diffs))
    return diffs


# ── Private helpers ───────────────────────────────────────────────────────────

def _compare_sheet_list(
    src_sheets: List[SheetInfo],
    tgt_sheets: List[SheetInfo],
) -> List[SheetDiff]:
    diffs: List[SheetDiff] = []
    src_map = {s.name: s for s in src_sheets}
    tgt_map = {s.name: s for s in tgt_sheets}

    for name in set(src_map) - set(tgt_map):
        diffs.append(SheetDiff("MISSING_SHEET", name, "sheet", name, None, "ERROR"))
        logger.error("Sheet MISSING IN TARGET: '%s'", name)

    for name in set(tgt_map) - set(src_map):
        diffs.append(SheetDiff("EXTRA_SHEET", name, "sheet", None, name, "WARNING"))
        logger.warning("Sheet EXTRA IN TARGET: '%s'", name)

    for name in set(src_map) & set(tgt_map):
        diffs.extend(_compare_single_sheet(src_map[name], tgt_map[name]))

    return diffs


def _compare_single_sheet(src: SheetInfo, tgt: SheetInfo) -> List[SheetDiff]:
    diffs: List[SheetDiff] = []
    name = src.name

    def _chk(field: str, sv: Any, tv: Any, severity: str = "WARNING") -> None:
        if sv != tv:
            diffs.append(SheetDiff("ATTR_MISMATCH", name, field, sv, tv, severity))
            logger.warning("Sheet '%s' — %s: src=%s tgt=%s", name, field, sv, tv)

    _chk("sheet_type", src.sheet_type, tgt.sheet_type, "ERROR")

    # Fields used
    src_fields = {f["name"] for f in src.fields}
    tgt_fields = {f["name"] for f in tgt.fields}
    for fname in src_fields - tgt_fields:
        diffs.append(SheetDiff("MISSING_FIELD_ON_SHEET", name, "field", fname, None, "ERROR"))
    for fname in tgt_fields - src_fields:
        diffs.append(SheetDiff("EXTRA_FIELD_ON_SHEET", name, "field", None, fname, "WARNING"))

    # Calculations (formula drift)
    src_calcs = {
        f["name"]: f.get("formula")
        for f in src.fields if f.get("formula") is not None
    }
    tgt_calcs = {
        f["name"]: f.get("formula")
        for f in tgt.fields if f.get("formula") is not None
    }
    for cname in set(src_calcs) & set(tgt_calcs):
        if src_calcs[cname] != tgt_calcs[cname]:
            diffs.append(SheetDiff(
                "CALC_CHANGED", name, f"calc.{cname}",
                src_calcs[cname], tgt_calcs[cname], "ERROR",
            ))
            logger.error("Sheet '%s' — calc '%s' formula changed.", name, cname)

    # Filters
    src_filter_fields = {f.field_name for f in src.filters}
    tgt_filter_fields = {f.field_name for f in tgt.filters}
    for ff in src_filter_fields - tgt_filter_fields:
        diffs.append(SheetDiff("MISSING_FILTER", name, "filter", ff, None, "WARNING"))
    for ff in tgt_filter_fields - src_filter_fields:
        diffs.append(SheetDiff("EXTRA_FILTER", name, "filter", None, ff, "WARNING"))

    return diffs


def _compare_dashboards(
    src_dashboards: List[DashboardInfo],
    tgt_dashboards: List[DashboardInfo],
) -> List[SheetDiff]:
    diffs: List[SheetDiff] = []
    src_map = {d.name: d for d in src_dashboards}
    tgt_map = {d.name: d for d in tgt_dashboards}

    for name in set(src_map) - set(tgt_map):
        diffs.append(SheetDiff("MISSING_DASHBOARD", name, "dashboard", name, None, "ERROR"))

    for name in set(tgt_map) - set(src_map):
        diffs.append(SheetDiff("EXTRA_DASHBOARD", name, "dashboard", None, name, "WARNING"))

    for name in set(src_map) & set(tgt_map):
        src_d = src_map[name]
        tgt_d = tgt_map[name]
        src_sheets = set(src_d.sheets)
        tgt_sheets = set(tgt_d.sheets)
        for s in src_sheets - tgt_sheets:
            diffs.append(SheetDiff("MISSING_SHEET_IN_DASHBOARD", name, "sheet", s, None, "WARNING"))
        for s in tgt_sheets - src_sheets:
            diffs.append(SheetDiff("EXTRA_SHEET_IN_DASHBOARD", name, "sheet", None, s, "WARNING"))

    return diffs


def _compare_parameters(
    src_params: List[ParameterInfo],
    tgt_params: List[ParameterInfo],
) -> List[SheetDiff]:
    diffs: List[SheetDiff] = []
    src_map = {p.name: p for p in src_params}
    tgt_map = {p.name: p for p in tgt_params}

    for name in set(src_map) - set(tgt_map):
        diffs.append(SheetDiff("MISSING_PARAMETER", name, "parameter", name, None, "WARNING"))

    for name in set(tgt_map) - set(src_map):
        diffs.append(SheetDiff("EXTRA_PARAMETER", name, "parameter", None, name, "WARNING"))

    for name in set(src_map) & set(tgt_map):
        sp, tp = src_map[name], tgt_map[name]
        if sp.data_type != tp.data_type:
            diffs.append(SheetDiff(
                "PARAM_TYPE_MISMATCH", name, "data_type",
                sp.data_type, tp.data_type, "ERROR",
            ))
        if sp.default_value != tp.default_value:
            diffs.append(SheetDiff(
                "PARAM_DEFAULT_MISMATCH", name, "default_value",
                sp.default_value, tp.default_value, "WARNING",
            ))

    return diffs
