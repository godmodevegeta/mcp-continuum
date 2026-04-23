# mcp_server/tests/test_e2e_workflows.py
import pytest
import respx
import httpx
import base64
from unittest.mock import MagicMock

# Import our tools
from server import (
    get_patient_clinical_profile,
    fetch_pathology_reports,
    fetch_radiology_reports,
    query_tumor_board_guidelines,
    save_tumor_board_note
)

# --- HELPER: Mock Prompt Opinion Context ---
def create_mock_context(url="https://mock-fhir.com", token="valid_token", patient_id="PT-123"):
    ctx = MagicMock()
    ctx.request_context.meta = {
        "X-FHIR-Server-URL": url,
        "X-FHIR-Access-Token": token,
        "X-Patient-ID": patient_id
    }
    return ctx

# --- SYNTHETIC FHIR DATA ---
MOCK_PATIENT_JSON = {"birthDate": "1965-04-12", "gender": "female"}
MOCK_CONDITIONS_JSON = {
    "entry":[{"resource": {"code": {"text": "Type 2 Diabetes Mellitus"}}}]
}

b64_pathology = base64.b64encode(b"Biopsy shows Invasive Ductal Carcinoma. ER Positive (90%). HER2 Negative. Ki-67 15%.").decode('utf-8')
MOCK_PATHOLOGY_JSON = {
    "entry":[{
        "resource": {
            "id": "path-1",
            "effectiveDateTime": "2026-04-10T10:00:00Z",
            "presentedForm": [{"contentType": "text/plain", "data": b64_pathology}]
        }
    }]
}

MOCK_RADIOLOGY_JSON = {
    "entry":[{
        "resource": {
            "id": "rad-1",
            "effectiveDateTime": "2026-04-15T14:30:00Z",
            "text": {"div": "<div>MRI Breast: 2.5cm mass in left breast. No axillary lymph node involvement (N0).</div>"}
        }
    }]
}

# ==========================================
# E2E WORKFLOW 1: THE HAPPY PATH (Complete MDT Prep)
# ==========================================
@pytest.mark.asyncio
@respx.mock
async def test_workflow_1_happy_path():
    # 1. Setup the intercepted routes (Mocking the FHIR Server)
    respx.get("https://mock-fhir.com/Patient/PT-123").mock(return_value=httpx.Response(200, json=MOCK_PATIENT_JSON))
    respx.get("https://mock-fhir.com/Condition?patient=PT-123&clinical-status=active").mock(return_value=httpx.Response(200, json=MOCK_CONDITIONS_JSON))
    respx.get("https://mock-fhir.com/DiagnosticReport?patient=PT-123&category=LP7839-6").mock(return_value=httpx.Response(200, json=MOCK_PATHOLOGY_JSON))
    respx.get("https://mock-fhir.com/DiagnosticReport?patient=PT-123&category=18748-4").mock(return_value=httpx.Response(200, json=MOCK_RADIOLOGY_JSON))
    respx.post("https://mock-fhir.com/DocumentReference").mock(return_value=httpx.Response(201, json={"id": "doc-999"}))

    ctx = create_mock_context()

    # Step 1: Coordinator fetches profile
    profile = await get_patient_clinical_profile(ctx)
    assert "1965-04-12" in profile
    assert "Type 2 Diabetes" in profile

    # Step 2: Pathology Agent fetches biopsy
    pathology = await fetch_pathology_reports(ctx)
    assert "Invasive Ductal Carcinoma" in pathology
    assert "ER Positive" in pathology

    # Step 3: Radiology Agent fetches imaging
    radiology = await fetch_radiology_reports(ctx)
    assert "2.5cm mass" in radiology
    assert "N0" in radiology

    # Step 4: Coordinator fetches guidelines (Local RAG)
    guidelines = await query_tumor_board_guidelines("Breast Cancer", "Stage IIA")
    assert "Lumpectomy" in guidelines or "Local Synced Cache" in guidelines

    # Step 5: Coordinator saves the final brief
    final_brief = f"""
    # MDT Brief
    {profile}
    ## Pathology
    {pathology}
    ## Radiology
    {radiology}
    ## Recommendations
    {guidelines}
    """
    save_result = await save_tumor_board_note(ctx, final_brief)
    
    # Verify the write-back succeeded
    assert "[SUCCESS]" in save_result
    assert "doc-999" in save_result


# ==========================================
# E2E WORKFLOW 2: THE "MISSING DATA" PATH
# ==========================================
@pytest.mark.asyncio
@respx.mock
async def test_workflow_2_empty_path():
    # Simulate a patient who hasn't had their biopsy or MRI yet
    respx.get("https://mock-fhir.com/DiagnosticReport?patient=PT-EMPTY&category=LP7839-6").mock(return_value=httpx.Response(200, json={"entry":[]}))
    respx.get("https://mock-fhir.com/DiagnosticReport?patient=PT-EMPTY&category=18748-4").mock(return_value=httpx.Response(200, json={"entry":[]}))

    ctx = create_mock_context(patient_id="PT-EMPTY")

    pathology = await fetch_pathology_reports(ctx)
    radiology = await fetch_radiology_reports(ctx)

    # Agents must gracefully report empty data, NOT crash
    assert "[EMPTY]" in pathology
    assert "[EMPTY]" in radiology


# ==========================================
# E2E WORKFLOW 3: THE "SYSTEM FAILURE" PATH
# ==========================================
@pytest.mark.asyncio
@respx.mock
async def test_workflow_3_system_failures():
    # 1. Missing SHARP Context (Unauthorized access attempt)
    bad_ctx = create_mock_context(url=None, token=None)
    profile_attempt = await get_patient_clinical_profile(bad_ctx)
    assert "[ERROR]" in profile_attempt
    assert "Missing SHARP FHIR context" in profile_attempt

    # 2. FHIR Server is Down (500 Error)
    respx.get("https://mock-fhir.com/DiagnosticReport?patient=PT-123&category=LP7839-6").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    
    valid_ctx = create_mock_context()
    pathology_attempt = await fetch_pathology_reports(valid_ctx)
    
    assert "[ERROR]" in pathology_attempt
    assert "500" in pathology_attempt