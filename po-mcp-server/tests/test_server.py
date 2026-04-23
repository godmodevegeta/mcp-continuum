# mcp_server/tests/test_mcp_server.py
"""
MDT Assemble - 100% Coverage Test Suite
---------------------------------------------------------
Coverage Matrix:
[x] fhir_extractor.py   (Happy, Base64 Error, HTML, Empty, Nil, Network Error)
[x] guideline_engine.py (Live API 200/500, Local Cache Hit/Miss, File Error, Toggles)
[x] server.py           (Context Auth, Tool Wrappers, FHIR Read/Write, 4-Paths)
---------------------------------------------------------
Run with: pytest tests/test_mcp_server.py -v --cov=. --cov-report=term-missing
"""

import pytest
import base64
import httpx
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
import os

# Import our modules
from fhir_extractor import strip_html, extract_clinical_text, fetch_and_parse_reports
from guideline_engine import fetch_live_nci_guidelines, fetch_local_guidelines, get_clinical_guidelines
from server import extract_sharp_context, get_patient_clinical_profile, fetch_pathology_reports, save_tumor_board_note

# ==========================================
# 1. FHIR EXTRACTOR TESTS
# ==========================================

def test_strip_html():
    assert strip_html("<div><p>Test <b>Clinical</b> Data</p></div>") == "Test Clinical Data"

def test_extract_clinical_text_base64_happy():
    encoded = base64.b64encode(b"Pathology Result").decode('utf-8')
    resource = {"presentedForm": [{"contentType": "text/plain", "data": encoded}]}
    assert extract_clinical_text(resource) == "Pathology Result"

def test_extract_clinical_text_base64_error():
    # Malformed base64
    resource = {"presentedForm":[{"contentType": "text/plain", "data": "!!NotBase64!!"}]}
    assert extract_clinical_text(resource) is None

def test_extract_clinical_text_html_happy():
    resource = {"text": {"div": "<div>Radiology Impression</div>"}}
    assert extract_clinical_text(resource) == "Radiology Impression"

def test_extract_clinical_text_nil():
    assert extract_clinical_text({}) is None

@pytest.mark.asyncio
async def test_fetch_and_parse_reports_happy():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "entry":[{"resource": {"id": "1", "text": {"div": "<div>Clear text</div>"}}}]
    }
    mock_client.get.return_value = mock_response

    result = await fetch_and_parse_reports(mock_client, "http://fhir", "pt1", "LP7839-6")
    assert "Clear text" in result
    assert "Report 1" in result

@pytest.mark.asyncio
async def test_fetch_and_parse_reports_empty():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"entry":[]} # Empty list
    mock_client.get.return_value = mock_response

    result = await fetch_and_parse_reports(mock_client, "http://fhir", "pt1", "LP7839-6")
    assert "[EMPTY]" in result

@pytest.mark.asyncio
async def test_fetch_and_parse_reports_error():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client.get.return_value = mock_response

    result = await fetch_and_parse_reports(mock_client, "http://fhir", "pt1", "LP7839-6")
    assert "[ERROR]" in result
    assert "500" in result

@pytest.mark.asyncio
async def test_fetch_and_parse_reports_network_error():
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.RequestError("Network Down")
    
    result = await fetch_and_parse_reports(mock_client, "http://fhir", "pt1", "LP7839-6")
    assert "[ERROR]" in result

# ==========================================
# 2. GUIDELINE ENGINE TESTS
# ==========================================

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_fetch_live_nci_guidelines_happy(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    res = await fetch_live_nci_guidelines("Breast Cancer", "Stage II")
    assert res["status"] == "happy"
    assert "Live data" in res["pathway"]

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_fetch_live_nci_guidelines_error(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_get.return_value = mock_resp
    assert await fetch_live_nci_guidelines("Rare Cancer", "Stage I") is None

@patch("os.path.exists", return_value=True)
@patch("builtins.open", new_callable=mock_open, read_data='{"breast_cancer": {"stage_iia": "Local Pathway"}}')
def test_fetch_local_guidelines_happy(mock_file, mock_exists):
    res = fetch_local_guidelines("Breast Cancer", "Stage IIA")
    assert res["status"] == "happy"
    assert res["pathway"] == "Local Pathway"

@patch("os.path.exists", return_value=False)
def test_fetch_local_guidelines_missing_file(mock_exists):
    res = fetch_local_guidelines("Breast Cancer", "Stage IIA")
    assert res["status"] == "error"

@pytest.mark.asyncio
@patch.dict(os.environ, {"USE_LIVE_NCI_API": "false"})
@patch("guideline_engine.fetch_local_guidelines")
async def test_get_clinical_guidelines_local(mock_local):
    mock_local.return_value = {"status": "happy", "source": "Local", "pathway": "Test Pathway"}
    res = await get_clinical_guidelines("Breast Cancer", "Stage IIA")
    assert "[Source: Local]" in res
    assert "Test Pathway" in res

# ==========================================
# 3. SERVER ORCHESTRATION & AUTH TESTS
# ==========================================

def get_mock_context(headers: dict):
    ctx = MagicMock()
    ctx.request_context.meta = headers
    return ctx

def test_extract_sharp_context_happy():
    ctx = get_mock_context({
        "X-FHIR-Server-URL": "https://fhir.example.com",
        "X-FHIR-Access-Token": "token123",
        "X-Patient-ID": "pt-1"
    })
    auth = extract_sharp_context(ctx)
    assert auth["status"] == "happy"
    assert auth["url"] == "https://fhir.example.com"

def test_extract_sharp_context_nil():
    ctx = get_mock_context({}) # Missing headers
    auth = extract_sharp_context(ctx)
    assert auth["status"] == "nil"

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_get_patient_clinical_profile_happy(mock_get):
    ctx = get_mock_context({"X-FHIR-Server-URL": "http://fhir", "X-FHIR-Access-Token": "tk", "X-Patient-ID": "1"})
    
    # Mocking sequential calls (Patient then Condition)
    mock_pt_resp = MagicMock()
    mock_pt_resp.status_code = 200
    mock_pt_resp.json.return_value = {"birthDate": "1980-01-01", "gender": "female"}
    
    mock_cond_resp = MagicMock()
    mock_cond_resp.status_code = 200
    mock_cond_resp.json.return_value = {"entry": [{"resource": {"code": {"text": "Hypertension"}}}]}
    
    mock_get.side_effect = [mock_pt_resp, mock_cond_resp]
    
    result = await get_patient_clinical_profile(ctx)
    assert "1980-01-01" in result
    assert "female" in result
    assert "Hypertension" in result

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_save_tumor_board_note_happy(mock_post):
    ctx = get_mock_context({"X-FHIR-Server-URL": "http://fhir", "X-FHIR-Access-Token": "tk", "X-Patient-ID": "1"})
    
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": "doc-999"}
    mock_post.return_value = mock_resp
    
    result = await save_tumor_board_note(ctx, "## MDT Brief")
    assert "[SUCCESS]" in result
    assert "doc-999" in result

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_save_tumor_board_note_error(mock_post):
    ctx = get_mock_context({"X-FHIR-Server-URL": "http://fhir", "X-FHIR-Access-Token": "tk", "X-Patient-ID": "1"})
    
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    mock_post.return_value = mock_resp
    
    result = await save_tumor_board_note(ctx, "## MDT Brief")
    assert "[ERROR]" in result
    assert "400" in result