# 🏥 MDT Assemble: Tumor Board Orchestrator (MCP Server)

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastMCP](https://img.shields.io/badge/Framework-FastMCP-brightgreen)
![FHIR R4](https://img.shields.io/badge/Standard-FHIR%20R4-orange)
![Hackathon](https://img.shields.io/badge/Prompt%20Opinion-Agents%20Assemble-purple)

**MDT Assemble** is an enterprise-grade, A2A-optimized Model Context Protocol (MCP) server. It bridges the gap between unstructured clinical data and specialized AI agents to orchestrate Multidisciplinary Tumor Board (MDT) preparations. 

By utilizing Prompt Opinion's SHARP context propagation, this server allows localized AI agents to extract pathology, interpret radiology, calculate deterministic staging, and write clinical briefs back to the EHR—saving oncology teams hours of unbillable administrative work.

---

## 🏗️ System Architecture

This server acts as the central data and logic hub for three specialized Prompt Opinion A2A Agents:

```text
[ Prompt Opinion SHARP Context (X-FHIR-Tokens) ]
                      │
   ┌──────────────────▼──────────────────┐
   │       MDT COORDINATOR AGENT         │ 
   │       (A2A Router & Synthesizer)    │
   └────────┬───────────────────┬────────┘
            │                   │ 
   ┌────────▼────────┐ ┌────────▼────────┐
   │ PATHOLOGY AGENT │ │ RADIOLOGY AGENT │ 
   └────────┬────────┘ └────────┬────────┘
════════════▼═══════════════════▼══════════════════
[ LOCAL PYTHON FASTMCP SERVER ]
   ├─ 📋 get_patient_clinical_profile()
   ├─ 🔬 fetch_pathology_reports() 
   ├─ 🩻 fetch_radiology_reports()  
   ├─ 🧮 calculate_clinical_stage() -> (Deterministic logic)
   ├─ 📚 query_tumor_board_guidelines()
   └─ 💾 save_tumor_board_note()   -> (FHIR Write-back)
```

---

## 🧰 MCP Tools List

This server exposes 6 purpose-built healthcare tools designed for 4-path error resilience (Happy, Nil, Empty, Error).

| Tool Name | Intended Agent | Description |
| :--- | :--- | :--- |
| `get_patient_clinical_profile` | **Coordinator** | Queries FHIR `Patient` and `Condition` resources to return demographics (Age, Sex) and active comorbidities. |
| `fetch_pathology_reports` | **Pathology** | Queries FHIR `DiagnosticReport` (LOINC `LP7839-6`), parses base64/HTML, and extracts raw tumor histology & biomarker text. |
| `fetch_radiology_reports` | **Radiology** | Queries FHIR `DiagnosticReport` (LOINC `18748-4`), parses base64/HTML, and extracts raw imaging narrative text. |
| `calculate_clinical_stage` | **Coordinator** | **Anti-Hallucination Guardrail:** A strictly deterministic Python function that calculates AJCC TNM Stage (Breast, Lung, Colorectal) based on extracted T/N/M values. |
| `query_tumor_board_guidelines` | **Coordinator** | Queries a local RAG JSON (fallback-ready for NCI PDQ Live API) to retrieve standard-of-care guidelines based on calculated stage. |
| `save_tumor_board_note` | **Coordinator** | Base64-encodes the final Markdown MDT brief and executes a `POST` request to the FHIR server, saving it as a preliminary `DocumentReference`. |

---

## 🚀 Installation & Setup

### 1. Prerequisites
Ensure you have Python 3.11+ installed.
```bash
# Clone the repository
git clone https://github.com/your-username/mdt-assemble-mcp.git
cd mdt-assemble-mcp

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows use `venv\Scripts\activate`

# Install dependencies
pip install mcp httpx starlette uvicorn pytest pytest-asyncio
```

### 2. Running the Server Locally
The server runs on SSE (Server-Sent Events) to maintain compatibility with the Prompt Opinion platform.

```bash
python server.py
# Server will start on http://127.0.0.1:8000/sse
```

### 3. Exposing via Ngrok (For Prompt Opinion)
In a second terminal window, expose your local port:
```bash
ngrok http 8000
```
*Copy the `https://<your-ngrok-id>.ngrok-free.app/sse` URL. This is your Endpoint URL for the Prompt Opinion UI.*

---

## 🧩 Prompt Opinion BYO Agent Configuration

To recreate the full "MDT Assemble" Cathedral workflow in your Prompt Opinion Workspace, create three **BYO Agents** and connect them to this MCP server via **Streamable HTTP**. 

> ⚠️ **Important:** Ensure the "Require FHIR Context" checkbox is **checked** when registering the MCP server so the SHARP headers inject correctly.

### 1. Pathology Agent (A2A)
*   **Tools:** `fetch_pathology_reports`
*   **System Prompt:** 
    > "You are an expert Oncologic Pathologist. Your sole responsibility is to extract unstructured pathology data from the FHIR record. When queried by the MDT Coordinator, fetch the reports, parse the text, and return a concise, structured summary of the histology, tumor grade, and critical biomarkers (e.g., ER/PR/HER2 for breast, EGFR/ALK/PD-L1 for lung). Do not attempt to stage the patient."

### 2. Radiology Agent (A2A)
*   **Tools:** `fetch_radiology_reports`
*   **System Prompt:** 
    > "You are an expert Oncologic Radiologist. Your sole responsibility is to extract unstructured imaging data from the FHIR record. When queried by the MDT Coordinator, fetch the reports and return a concise summary of the primary tumor size (T), regional lymph node involvement (N), and distant metastasis (M). Do not attempt to prescribe treatments."

### 3. MDT Coordinator (User-Facing)
*   **Tools:** `get_patient_clinical_profile`, `calculate_clinical_stage`, `query_tumor_board_guidelines`, `save_tumor_board_note`
*   **System Prompt:** 
    > "You are the Lead Medical Oncologist orchestrating a Multidisciplinary Tumor Board (MDT). When asked to prep a patient, execute these exact steps:
    > 1. Use your tool to fetch the patient's clinical profile (Age/Sex/Comorbidities).
    > 2. Query `@Pathology_Agent` via A2A to get the tumor histology and biomarkers. 
    > 3. Query `@Radiology_Agent` via A2A to get the TNM imaging findings. 
    > 4. Use the `calculate_clinical_stage` tool, passing the T, N, and M values provided by Radiology/Pathology to deterministically calculate the clinical stage. Do NOT guess the stage.
    > 5. Query the guidelines tool using the cancer type and calculated stage.
    > 6. Generate a final 'Tumor Board Brief' in Markdown.
    > 7. Automatically use the save tool to post this brief back to the FHIR record. Notify the user when complete."

---

## 🧪 Testing

The server includes a robust, 100% coverage `pytest` suite simulating upstream FHIR networks, SHARP context injection, HTML/Base64 edge cases, and deterministic staging permutations.

```bash
pytest tests/test_server.py -v --cov=. --cov-report=term-missing
```
---