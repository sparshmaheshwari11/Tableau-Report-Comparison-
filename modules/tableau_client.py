"""
modules/tableau_client.py
--------------------------
Authentication and low-level HTTP helpers for Tableau Server / Tableau Cloud.

Wraps:
  - tableauserverclient (TSC) for REST API operations
  - Raw requests.Session for Metadata API (GraphQL) + paginated REST calls
"""
import time
from typing import Any, Dict, Generator, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

import requests
import tableauserverclient as TSC

from modules.config_loader import AppConfig, TableauConfig
from modules.logger import setup_logger

logger = setup_logger(__name__)


class TableauClient:
    """
    Authenticated gateway to Tableau Server / Cloud.

    Usage:
        client = TableauClient(config)
        client.connect()
        workbooks = client.get_all_workbooks()
        client.disconnect()
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.tc = config.tableau
        self.perf = config.performance

        self._tsc_server: Optional[TSC.Server] = None
        self._auth_token: Optional[str] = None
        self._site_id: Optional[str] = None
        self._session: requests.Session = self._build_session()

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Authenticate with PAT and initialise TSC + raw HTTP session."""
        logger.info("Connecting to Tableau Server: %s", self.tc.server_url)

        tableau_auth = TSC.PersonalAccessTokenAuth(
            token_name=self.tc.pat_name,
            personal_access_token=self.tc.pat_value,
            site_id=self.tc.site_id,
        )
        server = TSC.Server(self.tc.server_url, use_server_version=False)
        server.version = self.tc.api_version

        for attempt in range(1, self.perf.max_retries + 1):
            try:
                server.auth.sign_in(tableau_auth)
                self._tsc_server = server
                self._auth_token = server.auth_token
                self._site_id = server.site_id
                self._session.headers.update(
                    {"x-tableau-auth": self._auth_token}
                )
                logger.info(
                    "Authenticated OK — site_id=%s  api_version=%s",
                    self._site_id,
                    self.tc.api_version,
                )
                return
            except Exception as exc:
                logger.warning(
                    "Auth attempt %d/%d failed: %s", attempt, self.perf.max_retries, exc
                )
                if attempt < self.perf.max_retries:
                    time.sleep(self.perf.retry_backoff_seconds * attempt)

        raise ConnectionError(
            f"Failed to authenticate to {self.tc.server_url} after {self.perf.max_retries} attempts."
        )

    def disconnect(self) -> None:
        if self._tsc_server:
            try:
                self._tsc_server.auth.sign_out()
                logger.info("Signed out of Tableau Server.")
            except Exception as exc:
                logger.warning("Sign-out error (non-fatal): %s", exc)
        self._session.close()

    # ── TSC convenience wrappers ──────────────────────────────────────────────

    @property
    def server(self) -> TSC.Server:
        if not self._tsc_server:
            raise RuntimeError("Not connected — call connect() first.")
        return self._tsc_server

    def get_workbook_by_id(self, workbook_id: str) -> TSC.WorkbookItem:
        wb = self.server.workbooks.get_by_id(workbook_id)
        logger.debug("Fetched workbook: %s (%s)", wb.name, workbook_id)
        return wb

    def get_workbook_views(self, workbook: TSC.WorkbookItem) -> List[TSC.ViewItem]:
        self.server.workbooks.populate_views(workbook)
        return list(workbook.views)

    def get_workbook_connections(self, workbook: TSC.WorkbookItem) -> List[TSC.ConnectionItem]:
        self.server.workbooks.populate_connections(workbook)
        return list(workbook.connections)

    def get_workbook_permissions(self, workbook: TSC.WorkbookItem):
        self.server.workbooks.populate_permissions(workbook)
        return workbook.permissions

    def get_all_workbooks(self) -> List[TSC.WorkbookItem]:
        """Return all workbooks on the site, handling TSC pagination."""
        all_wbs = []
        req_opts = TSC.RequestOptions(pagesize=self.config.comparison.pagination_page_size)
        for wb in TSC.Pager(self.server.workbooks, req_opts):
            all_wbs.append(wb)
        logger.info("Total workbooks on site: %d", len(all_wbs))
        return all_wbs

    # ── View data (paginated) ─────────────────────────────────────────────────

    def get_view_data_csv(self, view: TSC.ViewItem) -> bytes:
        """
        Download CSV data for a given view.
        Returns raw CSV bytes (handles Tableau's image vs data endpoints).
        """
        logger.debug("Fetching data for view: %s", view.name)
        self.server.views.populate_csv(view)
        return b"".join(view.csv)

    def get_view_data_json(self, view_id: str) -> Dict[str, Any]:
        """
        Fetch view underlying data via REST endpoint with pagination.
        Returns consolidated response dict.
        """
        endpoint = (
            f"{self.tc.server_url}/api/{self.tc.api_version}"
            f"/sites/{self._site_id}/views/{view_id}/data"
        )
        all_data: List[Dict] = []
        page = 1
        page_size = self.config.comparison.pagination_page_size
        total_available = None

        while True:
            params = {"pageSize": page_size, "pageNumber": page}
            resp = self._get_with_retry(endpoint, params=params)
            body = resp.json()

            records = body.get("data", [])
            all_data.extend(records)

            if total_available is None:
                pagination = body.get("pagination", {})
                total_available = int(pagination.get("totalAvailable", len(records)))
                logger.debug(
                    "View %s — total rows available: %d", view_id, total_available
                )

            fetched = page * page_size
            if fetched >= total_available or not records:
                break

            if fetched >= self.config.comparison.value_comparison_max_rows:
                logger.warning(
                    "View %s — reached max_rows cap (%d). Truncating.",
                    view_id,
                    self.config.comparison.value_comparison_max_rows,
                )
                break

            page += 1

        return {"data": all_data, "total_available": total_available}

    # ── Metadata API (GraphQL) ────────────────────────────────────────────────

    def metadata_query(self, graphql_query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Execute a GraphQL query against the Tableau Metadata API.

        IMPORTANT: If the server returns GraphQL validation errors
        (e.g. undefined fields due to older schema version), this method
        logs a WARNING and returns an empty dict instead of raising —
        allowing callers to gracefully fall back to TSC-only data.
        """
        endpoint = f"{self.tc.server_url}/api/metadata/graphql"
        payload: Dict[str, Any] = {"query": graphql_query}
        if variables:
            payload["variables"] = variables

        resp = self._post_with_retry(endpoint, json_body=payload)
        result = resp.json()

        if "errors" in result:
            errors = result["errors"]
            # Check if ALL errors are GraphQL validation errors (schema mismatch)
            # vs runtime errors (auth, network, etc.)
            all_validation = all(
                e.get("extensions", {}).get("classification") == "ValidationError"
                for e in errors
            )
            if all_validation:
                # Schema version mismatch — log and return empty so caller can fallback
                logger.warning(
                    "Metadata API schema validation errors (fields not supported in "
                    "this Tableau version). Returning empty result for graceful fallback.\n"
                    "Unsupported fields: %s",
                    [e.get("message", "")[:120] for e in errors],
                )
                return {}
            else:
                # Genuine runtime error — raise so caller knows something is wrong
                raise RuntimeError(f"Metadata API errors: {errors}")

        return result.get("data", {})

    def metadata_query_paginated(
        self,
        graphql_query_template: str,
        result_key: str,
        page_size: int = 100,
    ) -> Generator[Dict, None, None]:
        """
        Yields pages of results from a cursor-paginated Metadata API query.
        """
        cursor = None
        while True:
            variables = {"first": page_size, "after": cursor}
            data = self.metadata_query(graphql_query_template, variables)
            connection = data.get(result_key, {})
            edges = connection.get("edges", [])

            for edge in edges:
                yield edge.get("node", {})

            page_info = connection.get("pageInfo", {})
            if not page_info.get("hasNextPage", False):
                break
            cursor = page_info.get("endCursor")

    # ── URL → Workbook ID resolution ──────────────────────────────────────────

    def resolve_workbook_id_from_url(self, workbook_url: str) -> str:
        """
        Given a Tableau web URL (e.g. https://server/#/views/MyWB/Sheet1),
        return the workbook LUID via the REST API.
        Handles both /views/ and /workbooks/ URL patterns.
        Also strips query parameters like ?:origin=card_share_link
        """
        logger.info("Resolving workbook ID from URL: %s", workbook_url)

        # Strip query string from the full URL before parsing
        clean_url = workbook_url.split("?")[0]

        parsed = urlparse(clean_url)
        path_parts = [p for p in parsed.path.split("/") if p]
        fragment_parts = [p for p in (parsed.fragment or "").split("/") if p]
        all_parts = path_parts + fragment_parts

        workbook_name = None
        try:
            views_idx = all_parts.index("views")
            workbook_name = all_parts[views_idx + 1]
            workbook_name = workbook_name.split("?")[0]
        except (ValueError, IndexError):
            try:
                wb_idx = all_parts.index("workbooks")
                workbook_id = all_parts[wb_idx + 1]
                workbook_id = workbook_id.split("?")[0]
                logger.info("Resolved workbook LUID from URL: %s", workbook_id)
                return workbook_id
            except (ValueError, IndexError):
                pass

        if not workbook_name:
            raise ValueError(
                f"Cannot parse workbook identifier from URL: {workbook_url}"
            )

        # Search via REST
        req_opts = TSC.RequestOptions()
        req_opts.filter.add(
            TSC.Filter(TSC.RequestOptions.Field.ContentUrl, TSC.RequestOptions.Operator.Equals, workbook_name)
        )
        workbooks, _ = self.server.workbooks.get(req_opts)

        if not workbooks:
            req_opts2 = TSC.RequestOptions()
            req_opts2.filter.add(
                TSC.Filter(TSC.RequestOptions.Field.Name, TSC.RequestOptions.Operator.Equals, workbook_name)
            )
            workbooks, _ = self.server.workbooks.get(req_opts2)

        if not workbooks:
            raise ValueError(
                f"No workbook found matching name/url: '{workbook_name}' from URL: {workbook_url}"
            )

        if len(workbooks) > 1:
            logger.warning(
                "Multiple workbooks matched '%s' — using first result: %s",
                workbook_name,
                workbooks[0].id,
            )

        return workbooks[0].id

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        return session

    def _get_with_retry(self, url: str, params: Optional[Dict] = None) -> requests.Response:
        return self._request_with_retry("GET", url, params=params)

    def _post_with_retry(self, url: str, json_body: Optional[Dict] = None) -> requests.Response:
        return self._request_with_retry("POST", url, json_body=json_body)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        params: Optional[Dict] = None,
        json_body: Optional[Dict] = None,
    ) -> requests.Response:
        last_exc = None
        for attempt in range(1, self.perf.max_retries + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self.perf.request_timeout_seconds,
                )
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    logger.warning("401 Unauthorized — re-authenticating...")
                    self.connect()
                    continue
                last_exc = exc
            except requests.RequestException as exc:
                last_exc = exc

            logger.warning(
                "%s %s — attempt %d/%d failed: %s",
                method,
                url,
                attempt,
                self.perf.max_retries,
                last_exc,
            )
            if attempt < self.perf.max_retries:
                time.sleep(self.perf.retry_backoff_seconds * attempt)

        raise ConnectionError(
            f"HTTP {method} {url} failed after {self.perf.max_retries} attempts: {last_exc}"
        )