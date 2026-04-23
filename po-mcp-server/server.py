# po-mcp-server/server.py
"""
MDT Assemble - FastMCP Server
---------------------------------------------------------
ASCII Architecture Diagram:
[Prompt Opinion SHARP Context] -> extract_sharp_context()
       ├─ fetch_pathology_reports()   -> (Uses fhir_extractor)
       ├─ fetch_radiology_reports()   -> (Uses fhir_extractor)
       ├─ get_patient_clinical_profile() -> (Queries Patient/Condition)
       ├─ get_clinical_guidelines()   -> (Uses guideline_engine)
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

def extract_sharp_context(ctx: Context) -> dict:
    """
    Extracts the Prompt Opinion SHARP context headers securely.
    Handles the 4 paths: Happy, Nil (missing header), Empty, Error.
    """
    try:
        # Extract context from standard MCP metadata
        metadata = ctx.request_context.meta if hasattr(ctx, 'request_context') else {}
        fhir_url = metadata.get("X-FHIR-Server-URL")
        fhir_token = metadata.get("X-FHIR-Access-Token")
        patient_id = metadata.get("X-Patient-ID")

        if not fhir_url or not fhir_token or not patient_id:
            logger.warning("SHARP context missing from request")
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
            # Fetch Demographics
            pt_res = await client.get(f"{auth['url']}/Patient/{auth['patient_id']}", headers=headers)
            # Fetch Active Conditions
            cond_res = await client.get(f"{auth['url']}/Condition?patient={auth['patient_id']}&clinical-status=active", headers=headers)
            
            if pt_res.status_code != 200:
                return f"[ERROR] Upstream FHIR error fetching patient: {pt_res.status_code}"
                
            pt_data = pt_res.json()
            age = pt_data.get("birthDate", "Unknown (DOB missing)")
            gender = pt_data.get("gender", "Unknown")
            
            # Parse conditions
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
        # LOINC LP7839-6 = Pathology
        return await fetch_and_parse_reports(client, auth["url"], auth["patient_id"], "LP7839-6")


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
        # LOINC 18748-4 = Diagnostic Imaging
        return await fetch_and_parse_reports(client, auth["url"], auth["patient_id"], "18748-4")


@mcp.tool()
async def query_tumor_board_guidelines(cancer_type: str, clinical_stage: str) -> str:
    """
    Queries NCI PDQ / ASCO guidelines based on synthesized staging.
    Intended for the MDT Coordinator Agent.
    """
    # Calls our abstracted guideline engine (handles local RAG vs Live API toggle)
    return await get_clinical_guidelines(cancer_type, clinical_stage)


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
    
    # Base64 encode the markdown brief for FHIR compliance
    encoded_brief = base64.b64encode(markdown_brief.encode('utf-8')).decode('utf-8')
    
    # Construct FHIR R4 DocumentReference payload
    payload = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "preliminary", # Ensures it requires human MD sign-off
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
    logger.info("Starting MDT Assemble FastMCP Server on port 8000...")
    # Switch from stdio to sse and assign a port
    mcp.run(transport="sse")
