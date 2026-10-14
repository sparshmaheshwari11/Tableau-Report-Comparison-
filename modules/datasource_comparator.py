"""
modules/datasource_comparator.py
---------------------------------
Step 2 — Datasource validation.

Uses:
  - TSC connections API for connection-level metadata
  - Tableau Metadata API (GraphQL) for deep schema introspection
    (database, schema, table/view, field lineage, extract vs live)

NOTE: GraphQL query uses only fields universally available across
Tableau Cloud Metadata API versions (avoids connectionType on
PublishedDatasource, hostName on Database, dataType on Field
which are absent in older schema versions).
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import tableauserverclient as TSC

from modules.tableau_client import TableauClient
from modules.logger import setup_logger

logger = setup_logger(__name__)


# ── GraphQL Query (compatible with older Tableau Cloud Metadata API) ──────────

WORKBOOK_DATASOURCES_QUERY = """
query WorkbookDatasources($workbookId: String!) {
  workbooksConnection(filter: { luid: $workbookId }) {
    nodes {
      luid
      name
      embeddedDatasourcesConnection {
        nodes {
          id
          name
          hasExtracts
          extractLastRefreshTime
          extractLastUpdateTime
          upstreamDatasourcesConnection {
            nodes {
              luid
              name
              hasExtracts
              upstreamTablesConnection {
                nodes {
                  luid
                  name
                  schema
                  fullName
                  connectionType
                  database {
                    luid
                    name
                    connectionType
                  }
                }
              }
              fieldsConnection {
                nodes {
                  id
                  name
                  isHidden
                  description
                }
              }
            }
          }
          fieldsConnection {
            nodes {
              id
              name
              isHidden
              description
              ... on CalculatedField {
                formula
              }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass
class TableInfo:
    luid: str
    name: str
    schema: str
    full_name: str
    connection_type: str
    database_name: str
    database_host: str


@dataclass
class FieldInfo:
    id: str
    name: str
    data_type: str
    description: str
    is_hidden: bool
    formula: Optional[str] = None


@dataclass
class DatasourceInfo:
    id: str
    name: str
    connection_type: str
    has_extracts: bool
    extract_last_refresh: Optional[str]
    tables: List[TableInfo]
    fields: List[FieldInfo]
    server_address: Optional[str] = None
    server_port: Optional[str] = None
    username: Optional[str] = None


@dataclass
class DatasourceDiff:
    diff_type: str
    datasource_name: str
    field: str
    source_value: Any
    target_value: Any
    severity: str


def fetch_datasource_metadata(
    client: TableauClient,
    workbook_id: str,
    tsc_connections: List[TSC.ConnectionItem],
) -> List[DatasourceInfo]:
    """
    Fetch comprehensive datasource metadata via Metadata API (GraphQL),
    enriched with TSC connection details.
    """
    logger.info("Fetching datasource metadata for workbook: %s", workbook_id)

    try:
        data = client.metadata_query(
            WORKBOOK_DATASOURCES_QUERY,
            variables={"workbookId": workbook_id},
        )
    except RuntimeError as exc:
        logger.warning(
            "Metadata API query failed (%s). "
            "Falling back to TSC connection-level metadata only.", exc
        )
        return _fallback_from_tsc_connections(tsc_connections)

    workbooks = data.get("workbooksConnection", {}).get("nodes", [])
    if not workbooks:
        logger.warning("No workbook metadata returned for ID: %s", workbook_id)
        return _fallback_from_tsc_connections(tsc_connections)

    wb_node = workbooks[0]
    embedded_ds = wb_node.get("embeddedDatasourcesConnection", {}).get("nodes", [])

    # Build lookup of connection details from TSC
    conn_lookup: Dict[str, TSC.ConnectionItem] = {
        c.datasource_name: c for c in tsc_connections if c.datasource_name
    }
    conn_type_lookup: Dict[str, str] = {}
    for c in tsc_connections:
        if c.datasource_name:
            conn_type_lookup[c.datasource_name] = getattr(c, "connection_type", "") or ""

    datasources: List[DatasourceInfo] = []

    for ds_node in embedded_ds:
        ds_name = ds_node.get("name", "")
        has_extracts = ds_node.get("hasExtracts", False)
        extract_refresh = ds_node.get("extractLastRefreshTime")

        upstream_ds = ds_node.get("upstreamDatasourcesConnection", {}).get("nodes", [])
        tables: List[TableInfo] = []
        upstream_fields: List[FieldInfo] = []
        connection_type = conn_type_lookup.get(ds_name, "unknown")

        for ups in upstream_ds:
            for tbl in ups.get("upstreamTablesConnection", {}).get("nodes", []):
                db = tbl.get("database") or {}
                tables.append(TableInfo(
                    luid=tbl.get("luid", ""),
                    name=tbl.get("name", ""),
                    schema=tbl.get("schema", ""),
                    full_name=tbl.get("fullName", ""),
                    connection_type=tbl.get("connectionType", connection_type),
                    database_name=db.get("name", ""),
                    database_host=db.get("connectionType", ""),
                ))
            for fld in ups.get("fieldsConnection", {}).get("nodes", []):
                upstream_fields.append(_parse_field(fld))

        embedded_fields: List[FieldInfo] = [
            _parse_field(f)
            for f in ds_node.get("fieldsConnection", {}).get("nodes", [])
        ]
        all_fields = upstream_fields + embedded_fields

        tsc_conn = conn_lookup.get(ds_name)

        datasources.append(DatasourceInfo(
            id=ds_node.get("id", ""),
            name=ds_name,
            connection_type=connection_type,
            has_extracts=has_extracts,
            extract_last_refresh=extract_refresh,
            tables=tables,
            fields=all_fields,
            server_address=getattr(tsc_conn, "server_address", None) if tsc_conn else None,
            server_port=str(getattr(tsc_conn, "server_port", None)) if tsc_conn else None,
            username=getattr(tsc_conn, "username", None) if tsc_conn else None,
        ))
        logger.debug(
            "DS='%s' type='%s' extracts=%s tables=%d fields=%d",
            ds_name, connection_type, has_extracts, len(tables), len(all_fields),
        )

    logger.info(
        "Workbook %s — %d embedded datasource(s) found.", workbook_id, len(datasources)
    )
    return datasources


def _fallback_from_tsc_connections(
    tsc_connections: List[TSC.ConnectionItem],
) -> List[DatasourceInfo]:
    """
    Build minimal DatasourceInfo objects from TSC connection metadata alone.
    Used when the Metadata API is unavailable or returns errors.
    """
    logger.info(
        "Building datasource info from TSC connections only (%d connections).",
        len(tsc_connections),
    )
    datasources = []
    for conn in tsc_connections:
        name = getattr(conn, "datasource_name", "") or getattr(conn, "id", "unknown")
        datasources.append(DatasourceInfo(
            id=getattr(conn, "id", ""),
            name=name,
            connection_type=getattr(conn, "connection_type", "unknown") or "unknown",
            has_extracts=False,
            extract_last_refresh=None,
            tables=[],
            fields=[],
            server_address=getattr(conn, "server_address", None),
            server_port=str(getattr(conn, "server_port", None)),
            username=getattr(conn, "username", None),
        ))
    return datasources


def compare_datasources(
    src_datasources: List[DatasourceInfo],
    tgt_datasources: List[DatasourceInfo],
) -> List[DatasourceDiff]:
    """
    Full comparison of datasource metadata between source and target workbooks.
    """
    diffs: List[DatasourceDiff] = []

    src_map = {ds.name: ds for ds in src_datasources}
    tgt_map = {ds.name: ds for ds in tgt_datasources}

    for name in set(src_map) - set(tgt_map):
        diffs.append(DatasourceDiff(
            diff_type="MISSING_IN_TARGET",
            datasource_name=name,
            field="datasource",
            source_value=name,
            target_value=None,
            severity="ERROR",
        ))
        logger.error("Datasource MISSING IN TARGET: '%s'", name)

    for name in set(tgt_map) - set(src_map):
        diffs.append(DatasourceDiff(
            diff_type="EXTRA_IN_TARGET",
            datasource_name=name,
            field="datasource",
            source_value=None,
            target_value=name,
            severity="WARNING",
        ))
        logger.warning("Datasource EXTRA IN TARGET: '%s'", name)

    for name in set(src_map) & set(tgt_map):
        src = src_map[name]
        tgt = tgt_map[name]
        ds_diffs = _compare_single_datasource(src, tgt)
        diffs.extend(ds_diffs)

    logger.info("Datasource comparison complete — %d difference(s).", len(diffs))
    return diffs


def _compare_single_datasource(
    src: DatasourceInfo,
    tgt: DatasourceInfo,
) -> List[DatasourceDiff]:
    diffs: List[DatasourceDiff] = []
    name = src.name

    def _chk(field: str, sv: Any, tv: Any, severity: str = "WARNING") -> None:
        if sv != tv:
            diffs.append(DatasourceDiff(
                diff_type="ATTRIBUTE_MISMATCH",
                datasource_name=name,
                field=field,
                source_value=sv,
                target_value=tv,
                severity=severity,
            ))
            logger.warning("DS '%s' — %s:  src=%s  tgt=%s", name, field, sv, tv)

    _chk("connection_type", src.connection_type, tgt.connection_type, "ERROR")
    _chk("has_extracts",    src.has_extracts,    tgt.has_extracts,    "WARNING")
    _chk("server_address",  src.server_address,  tgt.server_address,  "WARNING")
    _chk("server_port",     src.server_port,     tgt.server_port,     "INFO")

    # Tables
    src_tables = {t.full_name or t.name: t for t in src.tables}
    tgt_tables = {t.full_name or t.name: t for t in tgt.tables}

    for tname in set(src_tables) - set(tgt_tables):
        diffs.append(DatasourceDiff("MISSING_TABLE", name, "table", tname, None, "ERROR"))
        logger.error("DS '%s' — table MISSING IN TARGET: '%s'", name, tname)

    for tname in set(tgt_tables) - set(src_tables):
        diffs.append(DatasourceDiff("EXTRA_TABLE", name, "table", None, tname, "WARNING"))

    for tname in set(src_tables) & set(tgt_tables):
        st = src_tables[tname]
        tt = tgt_tables[tname]
        if st.schema != tt.schema:
            diffs.append(DatasourceDiff(
                "TABLE_SCHEMA_MISMATCH", name, f"table.{tname}.schema",
                st.schema, tt.schema, "ERROR",
            ))
        if st.database_name != tt.database_name:
            diffs.append(DatasourceDiff(
                "TABLE_DATABASE_MISMATCH", name, f"table.{tname}.database_name",
                st.database_name, tt.database_name, "ERROR",
            ))

    # Fields
    src_fields = {f.name: f for f in src.fields}
    tgt_fields = {f.name: f for f in tgt.fields}

    for fname in set(src_fields) - set(tgt_fields):
        diffs.append(DatasourceDiff("MISSING_FIELD", name, fname, fname, None, "ERROR"))

    for fname in set(tgt_fields) - set(src_fields):
        diffs.append(DatasourceDiff("EXTRA_FIELD", name, fname, None, fname, "WARNING"))

    for fname in set(src_fields) & set(tgt_fields):
        sf = src_fields[fname]
        tf = tgt_fields[fname]
        if sf.data_type and tf.data_type and sf.data_type != tf.data_type:
            diffs.append(DatasourceDiff(
                "FIELD_TYPE_MISMATCH", name, f"field.{fname}.dataType",
                sf.data_type, tf.data_type, "ERROR",
            ))
        if sf.formula is not None and tf.formula is not None and sf.formula != tf.formula:
            diffs.append(DatasourceDiff(
                "CALC_FORMULA_MISMATCH", name, f"field.{fname}.formula",
                sf.formula, tf.formula, "ERROR",
            ))

    return diffs


def _parse_field(node: Dict) -> FieldInfo:
    return FieldInfo(
        id=node.get("id", ""),
        name=node.get("name", ""),
        data_type=node.get("dataType", ""),
        description=node.get("description", ""),
        is_hidden=bool(node.get("isHidden", False)),
        formula=node.get("formula"),
    )