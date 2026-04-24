# mcp_server/server.py
"""
MDT Assemble - FastMCP Server
---------------------------------------------------------
ASCII Architecture Diagram:
[Prompt Opinion SHARP Context] -> extract_sharp_context()
       ├─ fetch_pathology_reports()   -> (Uses fhir_extractor)
       ├─ fetch_radiology_reports()   -> (Uses fhir_extractor)
       ├─ get_patient_clinical_profile() -> (Queries Patient/Condition)
       ├─ calculate_clinical_stage() -> (Anti-Hallucination Guardrail, calculates AJCC TNM Stage)
       ├─ query_tumor_board_guidelines() -> (Uses guideline_engine)
       └─ save_tumor_board_note()     -> (POST DocumentReference to FHIR)
---------------------------------------------------------
"""

import base64
import logging
import httpx
import datetime
from mcp.server.fastmcp import FastMCP, Context

# Import our custom engines
from fhir_extractor import fetch_and_parse_reports
from guideline_engine import get_clinical_guidelines

# Observability first-class
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mdt-mcp-server")

mcp = FastMCP("MDT_Assemble_MCP", dependencies=["httpx"])

# --- THE HACKATHON GLOBAL STATE ---
# Bypasses async task boundaries to share headers between the POST request and SSE execution task.
LAST_FHIR_CONTEXT = {}

def extract_sharp_context(ctx: Context) -> dict:
    """
    Extracts the Prompt Opinion SHARP context headers securely.
    Handles the 4 paths: Happy, Nil (missing header), Empty, Error.
    """
    try:
        global LAST_FHIR_CONTEXT
        fhir_url = LAST_FHIR_CONTEXT.get("x-fhir-server-url")
        fhir_token = LAST_FHIR_CONTEXT.get("x-fhir-access-token")
        patient_id = LAST_FHIR_CONTEXT.get("x-patient-id")

        if not fhir_url or not fhir_token or not patient_id:
            logger.warning(f"SHARP context missing. Global headers: {list(LAST_FHIR_CONTEXT.keys())}")
            return {"status": "nil", "error": "Missing SHARP FHIR context headers. Ensure patient context is active."}
        
        return {
            "status": "happy",
            "url": fhir_url.rstrip('/'),
            "token": fhir_token,
            "patient_id": patient_id
        }
    except Exception as e:
        logger.error(f"Context extraction error: {str(e)}")
        return {"status": "error", "error": f"Internal Context Error: {str(e)}"}


@mcp.tool()
async def get_patient_clinical_profile(ctx: Context) -> str:
    """
    Fetches basic patient demographics (Age, Sex) and active comorbidities.
    Intended for the MDT Coordinator Agent.
    """
    auth = extract_sharp_context(ctx)
    if auth["status"] != "happy":
        return f"[ERROR] {auth.get('error')}"
    
    headers = {"Authorization": f"Bearer {auth['token']}", "Accept": "application/fhir+json"}
    
    try:
        async with httpx.AsyncClient() as client:
            pt_res = await client.get(f"{auth['url']}/Patient/{auth['patient_id']}", headers=headers)
            cond_res = await client.get(f"{auth['url']}/Condition?patient={auth['patient_id']}&clinical-status=active", headers=headers)
            
            if pt_res.status_code != 200:
                return f"[ERROR] Upstream FHIR error fetching patient: {pt_res.status_code}"
                
            pt_data = pt_res.json()
            age = pt_data.get("birthDate", "Unknown (DOB missing)")
            gender = pt_data.get("gender", "Unknown")
            
            conditions =[]
            if cond_res.status_code == 200:
                cond_data = cond_res.json()
                for entry in cond_data.get("entry",[]):
                    code_display = entry.get("resource", {}).get("code", {}).get("text", "Unknown Condition")
                    conditions.append(code_display)
            
            cond_str = ", ".join(conditions) if conditions else "None documented."
            return f"Patient Profile:\nDOB: {age}\nSex: {gender}\nActive Comorbidities: {cond_str}"
            
    except httpx.RequestError as e:
        logger.error(f"Network error: {str(e)}")
        return "[ERROR] Could not connect to FHIR server."


@mcp.tool()
async def fetch_pathology_reports(ctx: Context) -> str:
    """
    Fetches unstructured pathology reports for the current patient.
    Intended strictly for the Pathology Agent.
    """
    auth = extract_sharp_context(ctx)
    if auth["status"] != "happy":
        return f"[ERROR] {auth.get('error')}"
    
    async with httpx.AsyncClient() as client:
        return await fetch_and_parse_reports(client, auth["url"], auth["token"], auth["patient_id"], "LP7839-6")


@mcp.tool()
async def fetch_radiology_reports(ctx: Context) -> str:
    """
    Fetches unstructured radiology and imaging reports for the current patient.
    Intended strictly for the Radiology Agent.
    """
    auth = extract_sharp_context(ctx)
    if auth["status"] != "happy":
        return f"[ERROR] {auth.get('error')}"
    
    async with httpx.AsyncClient() as client:
        return await fetch_and_parse_reports(client, auth["url"], auth["token"], auth["patient_id"], "18748-4")


@mcp.tool()
async def query_tumor_board_guidelines(cancer_type: str, clinical_stage: str) -> str:
    """
    Queries NCI PDQ / ASCO guidelines based on synthesized staging.
    Intended for the MDT Coordinator Agent.
    """
    return await get_clinical_guidelines(cancer_type, clinical_stage)

@mcp.tool()
async def calculate_clinical_stage(cancer_type: str, t_stage: str, n_stage: str, m_stage: str) -> str:
    """
    Deterministically calculates the clinical stage based on AJCC TNM criteria.
    Supports Breast, NSCLC (Lung), and Colorectal cancer.
    Use this to prevent LLM hallucinations during staging.
    
    Args:
        cancer_type: The type of cancer (e.g., 'Breast Cancer', 'NSCLC', 'Colorectal')
        t_stage: Primary tumor size/extent extracted from radiology/pathology (e.g., 'T1', 'T2', 'T3', 'T4')
        n_stage: Lymph node involvement extracted from radiology/pathology (e.g., 'N0', 'N1', 'N2', 'N3')
        m_stage: Distant metastasis extracted from radiology (e.g., 'M0', 'M1')
    """
    logger.info(f"Deterministically calculating stage for {cancer_type}: {t_stage}, {n_stage}, {m_stage}")
    
    # Standardize inputs
    t = t_stage.upper().strip()
    n = n_stage.upper().strip()
    m = m_stage.upper().strip()
    ctype = cancer_type.lower()
    
    # Extract base T/N/M components (e.g., 'T1c' -> 'T1', 'N2a' -> 'N2')
    # This prevents the calculator from breaking if the LLM extracts sub-stages
    t_base = t[:2] if len(t) >= 2 else t
    n_base = n[:2] if len(n) >= 2 else n
    m_base = m[:2] if len(m) >= 2 else m

    # Any metastasis is universally Stage IV across all solid tumors
    if m_base == "M1":
        return "Stage IV"
        
    # 1. BREAST CANCER (AJCC 8th Edition - Simplified Anatomical)
    if "breast" in ctype:
        if t_base == "T1" and n_base == "N0": return "Stage IA"
        if t_base == "T2" and n_base == "N0": return "Stage IIA"
        if t_base == "T1" and n_base == "N1": return "Stage IIA"
        if t_base == "T2" and n_base == "N1": return "Stage IIB"
        if t_base == "T3" and n_base == "N0": return "Stage IIB"
        if t_base == "T3" and n_base == "N1": return "Stage IIIA"
        if n_base in ["N2", "N3"] or t_base == "T4": return "Stage IIIC"
        
    # 2. NON-SMALL CELL LUNG CANCER (NSCLC)
    elif "lung" in ctype or "nsclc" in ctype:
        if t_base == "T1" and n_base == "N0": return "Stage IA"
        if t_base == "T2" and n_base == "N0": return "Stage IB"
        if t_base in ["T1", "T2"] and n_base == "N1": return "Stage IIB"
        if t_base == "T3" and n_base == "N0": return "Stage IIB"
        if t_base in["T1", "T2"] and n_base == "N2": return "Stage IIIA"
        if t_base == "T3" and n_base == "N1": return "Stage IIIA"
        if t_base == "T4" and n_base in ["N0", "N1"]: return "Stage IIIA"
        if n_base == "N3": return "Stage IIIB"
        
    # 3. COLORECTAL CANCER
    elif "colon" in ctype or "colorectal" in ctype or "rectal" in ctype:
        if t_base in ["T1", "T2"] and n_base == "N0": return "Stage I"
        if t_base in["T3", "T4"] and n_base == "N0": return "Stage II"
        # Node positive (N1 or N2) without metastasis is universally Stage III
        if n_base in ["N1", "N2"]: return "Stage III" 

    # Fallback for unsupported combos or missing data
    logger.warning(f"Fallback triggered for {cancer_type}: {t_base}{n_base}{m_base}")
    return f"[WARNING] Could not deterministically calculate stage for {cancer_type} with {t_stage} {n_stage} {m_stage}. Requires human oncologist review."

@mcp.tool()
async def save_tumor_board_note(ctx: Context, markdown_brief: str) -> str:
    """
    Saves the final synthesized Tumor Board brief back to the EHR as a FHIR DocumentReference.
    Intended for the MDT Coordinator Agent.
    """
    auth = extract_sharp_context(ctx)
    if auth["status"] != "happy":
        return f"[ERROR] {auth.get('error')}"
    
    headers = {
        "Authorization": f"Bearer {auth['token']}", 
        "Content-Type": "application/fhir+json",
        "Accept": "application/fhir+json"
    }
    
    encoded_brief = base64.b64encode(markdown_brief.encode('utf-8')).decode('utf-8')
    
    payload = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "preliminary", 
        "type": {
            "coding":[{"system": "http://loinc.org", "code": "81215-6", "display": "Multidisciplinary team conference report"}]
        },
        "subject": {"reference": f"Patient/{auth['patient_id']}"},
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "content":[{
            "attachment": {
                "contentType": "text/markdown",
                "data": encoded_brief,
                "title": "AI-Synthesized Tumor Board Prep Brief"
            }
        }]
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{auth['url']}/DocumentReference", json=payload, headers=headers, timeout=10.0)
            
            if response.status_code in [200, 201]:
                res_json = response.json()
                doc_id = res_json.get("id", "Unknown ID")
                logger.info(f"Successfully saved MDT brief {doc_id} to FHIR.")
                return f"[SUCCESS] Tumor Board brief saved successfully to patient chart. (Document ID: {doc_id})"
            else:
                logger.error(f"FHIR write error: {response.text}")
                return f"[ERROR] Failed to save note to EHR. Server returned {response.status_code}."
                
    except httpx.RequestError as e:
        logger.error(f"Network error writing to FHIR: {str(e)}")
        return "[ERROR] Could not connect to FHIR server to save note."


if __name__ == "__main__":
    logger.info("Starting MDT Assemble FastMCP Server on SSE...")
    
    from starlette.applications import Starlette
    from mcp.types import InitializeResult
    import json
    
    original_call = Starlette.__call__
    
    async def patched_call(self, scope, receive, send):
        if scope["type"] == "http":
            headers =[]
            headers_dict = {}
            for k, v in scope.get("headers",[]):
                key_str = k.decode('utf-8').lower()
                val_str = v.decode('utf-8')
                headers_dict[key_str] = val_str
                
                # Trick Starlette into bypassing CSRF
                if k.lower() == b"host":
                    headers.append((b"host", b"127.0.0.1:8000"))
                elif k.lower() == b"accept" and scope["path"].rstrip("/") == "/sse":
                    headers.append((b"accept", b"text/event-stream"))
                else:
                    headers.append((k, v))
            
            if scope["path"].rstrip("/") == "/sse" and not any(k.lower() == b"accept" for k, v in headers):
                headers.append((b"accept", b"text/event-stream"))
                
            scope["headers"] = headers
            
            # --- GLOBAL HEADER EXTRACTION ---
            # If the request contains FHIR headers, store them globally!
            if "x-fhir-server-url" in headers_dict:
                global LAST_FHIR_CONTEXT
                LAST_FHIR_CONTEXT = headers_dict
                
            async def patched_send(message):
                if message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    if b"capabilities" in body and b"jsonrpc" in body and b"data:" in body:
                        try:
                            text = body.decode("utf-8")
                            new_text = ""
                            for line in text.split("\n"):
                                if line.startswith("data: ") and '"capabilities"' in line:
                                    payload = json.loads(line[6:])
                                    if "result" in payload and "capabilities" in payload["result"]:
                                        caps = payload["result"]["capabilities"]
                                        if "extensions" not in caps:
                                            caps["extensions"] = {}
                                        caps["extensions"]["ai.promptopinion/fhir-context"] = {
                                            "scopes":[
                                                {"name": "patient/Patient.rs", "required": True},
                                                {"name": "patient/Condition.rs", "required": True},
                                                {"name": "patient/DiagnosticReport.rs", "required": True},
                                                {"name": "patient/DocumentReference.write", "required": True}
                                            ]
                                        }
                                    new_text += "data: " + json.dumps(payload) + "\n"
                                else:
                                    new_text += line + "\n"
                            message["body"] = new_text[:-1].encode("utf-8")
                        except Exception as e:
                            logger.error(f"Failed to inject capabilities: {e}")
                await send(message)
            await original_call(self, scope, receive, patched_send)
        else:
            await original_call(self, scope, receive, send)
        
    Starlette.__call__ = patched_call

    # Start the server
    mcp.run(transport="sse")