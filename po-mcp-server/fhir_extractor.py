# po-mcp-server/fhir_extractor.py
import base64
import re
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("mdt-fhir-extractor")

def strip_html(raw_html: str) -> str:
    """Removes HTML tags from FHIR text.div payloads."""
    clean_text = re.sub(r'<.*?>', '', raw_html)
    return clean_text.strip()

def extract_clinical_text(resource: Dict) -> Optional[str]:
    """
    Safely extracts unstructured clinical text from a FHIR DiagnosticReport or DocumentReference.
    Handles base64 presentedForm and HTML text.div fallback.
    """
    # 1. Try presentedForm (Standard for raw Pathology/Radiology text)
    if "presentedForm" in resource:
        for form in resource["presentedForm"]:
            if form.get("contentType") in ["text/plain", "application/json"] and "data" in form:
                try:
                    decoded_bytes = base64.b64decode(form["data"])
                    return decoded_bytes.decode('utf-8')
                except Exception as e:
                    logger.error(f"Failed to decode base64 payload in resource {resource.get('id')}: {e}")
                    # Continue to fallback

    # 2. Try standard text.div fallback (Narrative text)
    if "text" in resource and "div" in resource["text"]:
        return strip_html(resource["text"]["div"])
        
    return None

async def fetch_and_parse_reports(client, fhir_url: str, patient_id: str, loinc_codes: str) -> str:
    """
    Executes the FHIR query and handles the 4-path data flow.
    loinc_codes: comma-separated string (e.g. '11526-1' for Path, '18748-4' for Rad)
    """
    query_url = f"{fhir_url}/DiagnosticReport?patient={patient_id}&category={loinc_codes}"
    
    try:
        response = await client.get(query_url, timeout=10.0)
        
        # Path 4: Upstream Error
        if response.status_code != 200:
            logger.error(f"FHIR upstream error: {response.status_code} on {query_url}")
            return f"[ERROR] FHIR server returned {response.status_code}."
            
        data = response.json()
        
        # Path 3: Empty
        if not data.get("entry") or len(data["entry"]) == 0:
            logger.info(f"Empty path: No reports found for {patient_id} with LOINCs {loinc_codes}")
            return "[EMPTY] No reports found matching these criteria in the patient's record."
            
        # Path 1: Happy
        extracted_reports = []
        for entry in data["entry"]:
            resource = entry.get("resource", {})
            rpt_id = resource.get('id', 'Unknown')
            date = resource.get('effectiveDateTime', 'Unknown Date')
            
            text_content = extract_clinical_text(resource)
            
            # Path 2: Nil (Resource exists, but text is null/unparseable)
            if not text_content:
                extracted_reports.append(f"--- Report {rpt_id} ({date}) ---\n[NIL] Text content missing or unparseable.\n")
            else:
                extracted_reports.append(f"--- Report {rpt_id} ({date}) ---\n{text_content}\n")
                
        return "\n".join(extracted_reports)
        
    except Exception as e:
        logger.error(f"Network/Parsing error: {str(e)}")
        return "[ERROR] Could not communicate with FHIR server or parse response."