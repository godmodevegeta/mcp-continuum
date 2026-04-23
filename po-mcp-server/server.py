# mcp_server/server.py
from mcp.server.fastmcp import FastMCP, Context
import httpx
import logging

# Observability first-class
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mdt-mcp-server")

# Initialize FastMCP Server
mcp = FastMCP("MDT_Assemble_MCP", dependencies=["httpx"])

def extract_sharp_context(ctx: Context) -> dict:
    """
    Extracts the Prompt Opinion SHARP context headers securely.
    Handles the 4 paths: Happy, Nil (missing header), Empty, Error.
    """
    # Note: FastMCP context retrieval pattern depends on the exact MCP SDK version,
    # but logically we are looking for X-FHIR-Server-URL and X-FHIR-Access-Token
    try:
        # Mocking context extraction based on standard MCP metadata payload
        metadata = ctx.request_context.meta if hasattr(ctx, 'request_context') else {}
        fhir_url = metadata.get("X-FHIR-Server-URL")
        fhir_token = metadata.get("X-FHIR-Access-Token")
        patient_id = metadata.get("X-Patient-ID")

        if not fhir_url or not fhir_token:
            logger.warning("SHARP context missing from request")
            return {"status": "nil", "error": "Missing SHARP FHIR context headers. Agent must enable FHIR context."}
        
        return {
            "status": "happy",
            "url": fhir_url,
            "token": fhir_token,
            "patient_id": patient_id
        }
    except Exception as e:
        logger.error(f"Context extraction error: {str(e)}")
        return {"status": "error", "error": str(e)}

@mcp.tool()
async def fetch_pathology_reports(ctx: Context) -> str:
    """
    Fetches unstructured pathology reports (DiagnosticReport/DocumentReference) for the current patient.
    Intended strictly for the Pathology Agent.
    """
    auth_state = extract_sharp_context(ctx)
    if auth_state["status"] != "happy":
        return f"System Error: {auth_state.get('error')}"
    
    patient_id = auth_state["patient_id"]
    headers = {"Authorization": f"Bearer {auth_state['token']}", "Accept": "application/fhir+json"}
    
    try:
        async with httpx.AsyncClient() as client:
            # FHIR R4 query for pathology reports
            response = await client.get(
                f"{auth_state['url']}/DiagnosticReport?patient={patient_id}&category=LP7839-6", # LOINC for Pathology
                headers=headers,
                timeout=10.0
            )
            
            if response.status_code != 200:
                logger.error(f"FHIR upstream error: {response.status_code}")
                return f"Upstream Error: FHIR server returned {response.status_code}."
            
            data = response.json()
            if not data.get("entry") or len(data["entry"]) == 0:
                logger.info(f"Empty path: No pathology reports found for {patient_id}")
                return "Empty Path: No pathology reports found for this patient."
                
            # Happy path extraction (simplified)
            # In a real build, we extract the base64 or presentedForm text
            reports = []
            for entry in data["entry"]:
                resource = entry.get("resource", {})
                reports.append(f"Report ID: {resource.get('id')} - Status: {resource.get('status')}")
                
            return "\n".join(reports)
            
    except httpx.RequestError as e:
        logger.error(f"Network error accessing FHIR: {str(e)}")
        return "Upstream Error: Could not connect to FHIR server."

@mcp.tool()
async def query_nci_pdq_guidelines(cancer_type: str, clinical_stage: str) -> str:
    """
    Queries open-access NCI PDQ or ASCO guidelines based on synthesized staging.
    Intended for the MDT Coordinator Agent.
    """
    # Implementation pending - we will hit an open public API here.
    return f"Mock Guideline for {cancer_type} at {clinical_stage}: Suggest multidisciplinary review for neoadjuvant options."

if __name__ == "__main__":
    logger.info("Starting MDT Assemble FastMCP Server...")
    mcp.run(transport="stdio") # Or SSE depending on deployment needs