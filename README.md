# Tableau Workbook Validator — Standalone Web App

A standalone HTML/CSS/JavaScript frontend with a Python FastAPI backend. It reuses the existing Tableau comparison engine in this repository (`modules/`, `main.py`, and report writers). The previous Streamlit UI has been removed; this app is launched with Uvicorn.

## User workflow

1. Enter Tableau Server/Cloud URL, site content URL (site ID), PAT name, PAT secret, and REST API version.
2. Select **Verify connection**.
3. After authentication succeeds, enter the original dashboard URL and updated dashboard URL.
4. Select whether to compare view data values and set numeric tolerance.
5. Run comparison.
6. Review workbook, datasource, sheet/dashboard, and value differences in the UI.
7. Download a ZIP containing the generated CSV, Excel, JSON report and validation log.

## Requirements

- Python 3.10 or newer (Python 3.11 recommended).
- Tableau Server or Tableau Cloud access.
- A Tableau Personal Access Token with permissions to view both workbooks and their views.
- Metadata API access for metadata checks; value checks depend on the view data being accessible through the Tableau REST API.

## Run locally on Windows / VS Code

### Option A — launch script
Double-click `start_windows.bat`, or run it from the VS Code terminal.

### Option B — manual commands
From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

## Tableau values

- **Tableau Server URL**: the site root, for example `https://tableau.example.com`. Do not enter the view URL in this field.
- **Site content URL / Site ID**: the site's content URL slug (not the friendly display name). For the Default site, leave blank.
- **PAT name / PAT secret**: the name and secret from the Tableau Personal Access Token.
- **Original dashboard URL / Updated dashboard URL**: the Tableau view links for the two versions. The authenticated account must be able to access both.

The UI first tests the credentials. The comparison endpoint then authenticates again to run the existing comparison pipeline. The PAT secret is not written into a persistent config file or browser storage. It is sent to this app's backend over the connection used by the browser.

## Deploying

This is a normal FastAPI app and can be deployed to a Python-capable host.

Build command:
```bash
pip install -r requirements.txt
```

Start command:
```bash
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

### Security before public deployment

This application handles sensitive Tableau credentials. Before exposing it to the public internet:
- Serve it only over HTTPS.
- Add application-level user authentication and authorization.
- Restrict access to trusted users/networks.
- Configure temporary report retention and cleanup.
- Do not log request bodies or PAT secrets.
- Consider a managed secrets/session architecture if multiple concurrent users will use the service.

The provided app is suitable for local development and controlled internal deployment; it should not be exposed as an unrestricted public endpoint.

## Existing comparison logic retained

The web application runs the repository's existing `main.py` pipeline, including:
- Workbook metadata comparison
- Datasource comparison
- Sheet/dashboard/parameter comparison
- View data/value comparison when enabled
- CSV, JSON, and Excel report generation

The exact findings depend on the Tableau Server/Cloud APIs, workbook permissions, metadata API availability, data source access, and the comparison logic implemented in the existing modules.

## Troubleshooting

- **Authentication failed**: verify the server root URL, site content URL, PAT name, PAT secret, and token expiry.
- **Workbook not found**: use a full Tableau view URL and ensure the authenticated user can see it. URL resolution uses the workbook identifier/name logic in `modules/tableau_client.py`.
- **Metadata differences missing**: check Metadata API availability and account permissions.
- **Value checks empty**: confirm the views are accessible and their data can be exported via Tableau REST API.
- **Port already in use**: start with `--port 8001` and open `http://127.0.0.1:8001`.

## Project structure

```text
tableau-workbook-validator-main/
├── backend/
│   └── main.py             # FastAPI endpoints and UI serving
├── static/
│   ├── index.html          # Frontend
│   ├── styles.css          # Light UI theme
│   └── app.js              # Form flow and results rendering
├── modules/                # Existing comparison modules
├── config/
├── main.py                 # Existing CLI comparison pipeline
├── requirements.txt
└── start_windows.bat
```
