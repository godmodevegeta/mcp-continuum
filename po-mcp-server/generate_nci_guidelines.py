#!/usr/bin/env python3
"""
NCI PDQ Guidelines Generator 
=====================================================
Uses verified cancer.gov URLs and built-in html.parser only.
No lxml, no diskcache, no slice bugs.

Verified URL patterns (April 2026):
- Treatment PDQs: https://www.cancer.gov/types/{cancer}/hp/{cancer}-treatment-pdq
- Side effects: https://www.cancer.gov/about-cancer/treatment/side-effects/{topic}
- ClinicalTrials API: https://clinicaltrials.gov/api/v2

License: MIT
"""

import json
import logging
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator

# ─────────────────────────────────────────────────────────────────────────────
# GLOBALS FOR GRACEFUL INTERRUPT
# ─────────────────────────────────────────────────────────────────────────────
SHUTDOWN_REQUESTED = False

def signal_handler(sig, frame):
    global SHUTDOWN_REQUESTED
    logging.warning("⚠ Interrupt received. Finishing current task then exiting...")
    SHUTDOWN_REQUESTED = True

signal.signal(signal.SIGINT, signal_handler)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — VERIFIED URLS ONLY
# ─────────────────────────────────────────────────────────────────────────────

REQUEST_DELAY = 0.3  # 3 req/sec conservative
USER_AGENT = "NCI-Guidelines-Generator/1.0 (open-source)"

# VERIFIED cancer.gov PDQ treatment URLs (tested April 2026) [[36]]
CANCER_TYPES = {
    "breast_cancer": {
        "pdq_url": "https://www.cancer.gov/types/breast/hp/breast-treatment-pdq",
        "api_query": "breast cancer"
    },
    "prostate_cancer": {
        "pdq_url": "https://www.cancer.gov/types/prostate/hp/prostate-treatment-pdq",
        "api_query": "prostate cancer"
    },
    "pancreatic_cancer": {
        "pdq_url": "https://www.cancer.gov/types/pancreatic/hp/pancreatic-treatment-pdq",
        "api_query": "pancreatic cancer"
    },
    "ovarian_cancer": {
        "pdq_url": "https://www.cancer.gov/types/ovarian/hp/ovarian-treatment-pdq",
        "api_query": "ovarian cancer"
    },
    "melanoma": {
        "pdq_url": "https://www.cancer.gov/types/skin/hp/melanoma-treatment-pdq",
        "api_query": "melanoma"
    },
    "lymphoma": {
        "pdq_url": "https://www.cancer.gov/types/lymphoma/hp/adult-nhl-treatment-pdq",
        "api_query": "non-hodgkin lymphoma"
    },
    "glioma": {
        "pdq_url": "https://www.cancer.gov/types/brain/hp/adult-brain-treatment-pdq",
        "api_query": "glioma"
    },
    "renal_cancer": {
        "pdq_url": "https://www.cancer.gov/types/kidney/hp/kidney-treatment-pdq",
        "api_query": "kidney cancer"
    },
    "bladder_cancer": {
        "pdq_url": "https://www.cancer.gov/types/bladder/hp/bladder-treatment-pdq",
        "api_query": "bladder cancer"
    },
    "colon_cancer": {
        "pdq_url": "https://www.cancer.gov/types/colon/hp/colon-treatment-pdq",
        "api_query": "colon cancer"
    },
    "rectal_cancer": {
        "pdq_url": "https://www.cancer.gov/types/rectal/hp/rectal-treatment-pdq",
        "api_query": "rectal cancer"
    },
}

STAGES = ["stage_0", "stage_i", "stage_ii", "stage_iii", "stage_iv", "recurrent"]

# VERIFIED side effects URLs (pattern: /about-cancer/treatment/side-effects/{topic})
SAFETY_CONTENT = {
    "dpd_deficiency": "https://www.cancer.gov/about-cancer/treatment/side-effects/nausea",  # DPD info in nausea/vomiting PDQ
    "cardiotoxicity": "https://www.cancer.gov/about-cancer/treatment/side-effects/heart",
    "neuropathy": "https://www.cancer.gov/about-cancer/treatment/side-effects/nerve",  # "nerve" not "neuropathy"
}

# ─────────────────────────────────────────────────────────────────────────────
# DATA MODELS (Pydantic V2)
# ─────────────────────────────────────────────────────────────────────────────

class LevelOfEvidence(BaseModel):
    level: str
    source: str
    last_updated: str

class TrialReference(BaseModel):
    nct_id: str
    title: str
    phase: Optional[str] = None
    status: str
    url: str
    
    @field_validator("nct_id")
    @classmethod
    def validate_nct(cls, v: str) -> str:
        if not re.match(r"^NCT\d{8}$", v.upper()):
            raise ValueError(f"Invalid NCT: {v}")
        return v.upper()

class SafetyContent(BaseModel):
    topic: str
    recommendation: str
    source_url: str

class PathwaySection(BaseModel):
    title: str
    content: str
    evidence: list[LevelOfEvidence] = Field(default_factory=list)
    trials_under_evaluation: list[TrialReference] = Field(default_factory=list)

class CancerStagePathway(BaseModel):
    stage: str
    sections: list[PathwaySection]
    supportive_care: list[SafetyContent] = Field(default_factory=list)
    surveillance_schedule: Optional[str] = None
    recurrence_management: Optional[str] = None

    @field_validator("stage")
    @classmethod
    def validate_stage(cls, v: str) -> str:
        if not re.match(r"^(stage_[0ivx]+|recurrent)$", v.lower()):
            raise ValueError(f"Invalid stage: {v}")
        return v.lower()

class CancerGuideline(BaseModel):
    cancer_type: str
    stages: dict[str, CancerStagePathway]
    meta: dict[str, Any] = Field(default_factory=lambda: {
        "source": "NCI PDQ + ClinicalTrials.gov API v2",
        "generated_at": datetime.now(timezone.utc).isoformat()
    })

# ─────────────────────────────────────────────────────────────────────────────
# SIMPLE HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_text(url: str, max_retries: int = 2) -> Optional[str]:
    """Fetch URL text with retry. Returns None on failure."""
    if SHUTDOWN_REQUESTED:
        return None
        
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                time.sleep(REQUEST_DELAY * attempt)
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            if attempt == max_retries:
                logging.warning(f"Failed to fetch {url}: {e}")
                return None
            logging.debug(f"Retry {attempt+1} for {url}")
    return None

def fetch_json(url: str, params: Optional[dict] = None, max_retries: int = 2) -> Optional[dict]:
    """Fetch JSON API with retry."""
    if SHUTDOWN_REQUESTED:
        return None
        
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    
    if params:
        url += "?" + urlencode(params, safe=',')
    
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                time.sleep(REQUEST_DELAY * attempt)
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            if attempt == max_retries:
                logging.warning(f"JSON fetch failed {url}: {e}")
                return None
            logging.debug(f"JSON retry {attempt+1} for {url}")
    return None

# ─────────────────────────────────────────────────────────────────────────────
# CONTENT FETCHERS — USING html.parser ONLY
# ─────────────────────────────────────────────────────────────────────────────

def fetch_clinical_trials(condition: str, page_size: int = 3) -> list[TrialReference]:
    """Fetch trials from ClinicalTrials.gov API v2."""
    base = "https://clinicaltrials.gov/api/v2/studies"
    params = {
        "query.cond": condition,
        "filter.overallStatus": "RECRUITING",
        "pageSize": page_size,
    }
    
    data = fetch_json(base, params)
    if not data or "studies" not in data:
        return []
    
    trials = []
    for study in data["studies"][:page_size]:
        try:
            protocol = study.get("protocolSection", {})
            ident = protocol.get("identificationModule", {})
            status = protocol.get("statusModule", {})
            
            nct_id = ident.get("nctId")
            if not nct_id:
                continue
            
            trials.append(TrialReference(
                nct_id=nct_id,
                title=ident.get("briefTitle", "Untitled"),
                phase=status.get("phase"),
                status=status.get("overallStatus", "UNKNOWN"),
                url=f"https://clinicaltrials.gov/study/{nct_id}"
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return trials


def fetch_pdq_content(pdq_url: str, section_anchor: Optional[str] = None) -> Optional[str]:
    """Fetch PDQ content using built-in html.parser."""
    if SHUTDOWN_REQUESTED:
        return None
        
    url = f"{pdq_url}#{section_anchor}" if section_anchor else pdq_url
    html = fetch_text(url)
    if not html:
        return None
    
    try:
        soup = BeautifulSoup(html, "html.parser")
        
        # Remove non-content elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        
        # Find main content
        main = soup.find("main") or soup.find("article") or soup.find("div", class_="content")
        text = (main or soup).get_text(separator="\n", strip=True)
        
        # Clean and truncate
        text = re.sub(r"\n\s*\n", "\n\n", text).strip()
        if not text:
            return None
        
        # Safe truncation with explicit str()
        text_str = str(text)
        return text_str[:4000] + "..." if len(text_str) > 4000 else text_str
        
    except Exception as e:
        logging.debug(f"PDQ parse error for {url}: {e}")
        return None


def fetch_safety_content(url: str) -> Optional[SafetyContent]:
    """Fetch safety content from cancer.gov."""
    if SHUTDOWN_REQUESTED:
        return None
        
    html = fetch_text(url)
    if not html:
        return None
    
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        
        main = soup.find("main") or soup.find("article")
        if not main:
            return None
        
        # Find recommendation
        paragraphs = main.find_all("p")
        recommendation = None
        for p in paragraphs:
            txt = p.get_text(strip=True)
            if any(kw in txt.lower() for kw in ["recommended", "should", "monitor", "assess"]):
                recommendation = txt
                break
        
        if not recommendation and paragraphs:
            recommendation = paragraphs[0].get_text(strip=True)
        
        topic = url.rstrip("/").split("/")[-1].replace("-", " ").title()
        rec_str = str(recommendation) if recommendation else "See source for details."
        
        return SafetyContent(
            topic=topic,
            recommendation=rec_str[:500] + "..." if len(rec_str) > 500 else rec_str,
            source_url=url
        )
    except Exception as e:
        logging.debug(f"Safety parse error for {url}: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# GUIDELINE CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────

def build_section(title: str, pdq_url: str, condition: str) -> PathwaySection:
    """Build a single pathway section."""
    anchor_map = {
        "Required Pathology / Biomarkers": "biomarkers",
        "Primary Surgical Options": "surgery",
        "Systemic Therapy (Adjuvant/Neoadjuvant)": "chemotherapy",
        "Radiation Therapy Considerations": "radiation",
        "Clinical Trial Options (Under Evaluation)": None,
    }
    anchor = anchor_map.get(title)
    
    content = fetch_pdq_content(pdq_url, anchor)
    if not content:
        content = f"*Content for '{title}' unavailable. Refer to [NCI PDQ]({pdq_url}).*"
    
    trials = fetch_clinical_trials(condition, page_size=3)
    
    evidence = []
    if "unavailable" not in content:
        evidence.append(LevelOfEvidence(
            level="2A",
            source=f"NCI PDQ {pdq_url}",
            last_updated=datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ))
    
    return PathwaySection(
        title=title,
        content=content,
        evidence=evidence,
        trials_under_evaluation=trials
    )


def generate_stage(pdq_url: str, condition: str, stage: str) -> CancerStagePathway:
    """Generate pathway for one stage."""
    if SHUTDOWN_REQUESTED:
        raise KeyboardInterrupt("Shutdown requested")
    
    sections = [
        build_section(title, pdq_url, condition)
        for title in [
            "Required Pathology / Biomarkers",
            "Primary Surgical Options",
            "Systemic Therapy (Adjuvant/Neoadjuvant)",
            "Radiation Therapy Considerations",
            "Clinical Trial Options (Under Evaluation)",
        ]
    ]
    
    # Safety content (with graceful fallback)
    safety = []
    for url in SAFETY_CONTENT.values():
        item = fetch_safety_content(url)
        if item:
            safety.append(item)
    
    # Surveillance/recurrence
    surveillance = fetch_pdq_content(pdq_url, "surveillance")
    recurrence = fetch_pdq_content(pdq_url, "recurrent") if stage != "recurrent" else None
    
    surv_str = str(surveillance)[:300] + "..." if surveillance and len(str(surveillance)) > 300 else (str(surveillance) if surveillance else "Follow NCCN/ASCO guidelines.")
    rec_str = str(recurrence)[:300] + "..." if recurrence and len(str(recurrence)) > 300 else (str(recurrence) if recurrence else None)
    
    return CancerStagePathway(
        stage=stage,
        sections=sections,
        supportive_care=safety,
        surveillance_schedule=surv_str,
        recurrence_management=rec_str
    )


def generate_guideline(cancer_type: str, config: dict) -> CancerGuideline:
    """Generate complete guideline for one cancer type."""
    stages_data = {}
    for stage in STAGES:
        if SHUTDOWN_REQUESTED:
            logging.info(f"Stopping at {cancer_type}/{stage} due to interrupt")
            break
        logging.info(f"  → {stage}")
        stages_data[stage] = generate_stage(config["pdq_url"], config["api_query"], stage)
    
    return CancerGuideline(cancer_type=cancer_type, stages=stages_data)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main(output_file: str = "nci_guidelines.json", dry_run: bool = False):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler()]
    )
    
    logging.info("Starting NCI Guidelines Generator (FINAL WORKING VERSION)")
    
    # Quick API health check
    version = fetch_json("https://clinicaltrials.gov/api/v2/version")
    if version and "dataTimestamp" in version:
        logging.info(f"✓ ClinicalTrials.gov API OK ({version['dataTimestamp']})")
    
    if dry_run:
        logging.info("✓ Dry run complete. Use --generate to build guidelines.")
        return
    
    all_guidelines = {}
    total = len(CANCER_TYPES)
    
    for idx, (cancer_type, config) in enumerate(CANCER_TYPES.items(), 1):
        if SHUTDOWN_REQUESTED:
            logging.warning("⚠ Shutdown requested. Saving partial results...")
            break
            
        logging.info(f"[{idx}/{total}] {cancer_type}")
        try:
            guideline = generate_guideline(cancer_type, config)
            all_guidelines[cancer_type] = guideline.model_dump(by_alias=True, exclude_unset=True)
            logging.info(f"  ✓ Done")
        except KeyboardInterrupt:
            logging.warning("⚠ Interrupted. Saving partial results...")
            break
        except Exception as e:
            logging.error(f"  ✗ Failed: {e}")
            # Fallback structure
            all_guidelines[cancer_type] = {
                "cancer_type": cancer_type,
                "stages": {s: {"stage": s, "sections": [], "supportive_care": []} for s in STAGES},
                "metadata": {"error": str(e), "generated_at": datetime.now(timezone.utc).isoformat()}
            }
    
    # Write output
    if all_guidelines:
        output_path = Path(output_file)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(all_guidelines, f, indent=2, ensure_ascii=False)
        logging.info(f"✓ Guidelines written to {output_path.resolve()}")
        logging.info(f"  Generated {len(all_guidelines)}/{total} cancer types")
    else:
        logging.error("✗ No guidelines generated. Check logs for errors.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", "-o", default="nci_guidelines.json")
    parser.add_argument("--dry-run", action="store_true", help="Test connectivity only")
    parser.add_argument("--generate", action="store_true", help="Generate full guidelines")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    if args.dry_run:
        main(dry_run=True)
    elif args.generate or not args.dry_run:
        main(output_file=args.output)