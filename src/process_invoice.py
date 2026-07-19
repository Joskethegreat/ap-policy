import os
import json
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from IPython.display import HTML, display
from supabase import create_client, Client

async def step_process_invoice_execution():
    # Namespaced variables for the step
    step_id = "process_invoice"
    step_proc_belnr = user_inputs.get("belnr")
    step_proc_input_run_id = user_inputs.get("run_id")
    
    print(f"Processing Invoice Document Number (belnr): {step_proc_belnr}")
    
    step_proc_final_output = {
        "ok": True,
        "exceptions": [],
        "notes": []
    }

    if not step_proc_belnr:
        step_proc_final_output["ok"] = False
        step_proc_final_output["exceptions"].append("MISSING_REQUIRED_FIELD")
        return

    step_proc_run_id = step_proc_input_run_id if step_proc_input_run_id and str(step_proc_input_run_id).strip() else str(uuid.uuid4())
    step_proc_policy = globals().get('policy', {})
    
    step_proc_sb_url = os.environ.get("SUPABASE_URL")
    step_proc_sb_key = os.environ.get("SUPABASE_SERVICE_KEY")
    
    if not step_proc_sb_url or not step_proc_sb_key:
        raise ValueError("Supabase configuration missing.")

    step_proc_supabase: Client = create_client(step_proc_sb_url, step_proc_sb_key)

    def step_proc_resolve_record(table_name: str, filters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        query = step_proc_supabase.table(table_name).select("*")
        for k, v in filters.items():
            query = query.eq(k, v)
        response = query.execute()
        data = response.data
        if not data: return None
        if len(data) == 1: return data[0]
        
        ref_cfg = step_proc_policy.get("reference_data", {})
        tie_breaker = ref_cfg.get("tie_breaker", "latest_erdat")
        if tie_breaker == "latest_erdat":
            def get_sort_key(x): return str(x.get("erdat") or x.get("created_at") or "")
            return sorted(data, key=get_sort_key, reverse=True)[0]
        return data[0]

    # 1. Lookup rbkp_invoice
    step_proc_raw_invoice = step_proc_resolve_record("rbkp_invoice", {"belnr": step_proc_belnr})
    if not step_proc_raw_invoice:
        step_proc_final_output["ok"] = False
        step_proc_final_output["exceptions"].append("MISSING_REQUIRED_FIELD")
        return

    # 2. Normalize Amount
    step_proc_wrbtr_raw = str(step_proc_raw_invoice.get("wrbtr") or "").strip()
    def step_proc_parse_amount(val_str: str) -> Optional[float]:
        if not val_str: return None
        s = val_str.replace(" ", "")
        try:
            if "." in s and "," in s: return float(s.replace(",", ""))
            if "," in s: return float(s.replace(",", "."))
            return float(s)
        except: return None

    step_proc_amount = step_proc_parse_amount(step_proc_wrbtr_raw)
    if step_proc_amount is None:
        step_proc_final_output["exceptions"].append("UNPARSEABLE_VALUE")
    
    # 3. Vendor Check
    step_proc_lifnr = str(step_proc_raw_invoice.get("lifnr") or "").strip()
    step_proc_vendor_name = "Unknown Vendor"
    if step_proc_lifnr:
        step_proc_vendor_rec = step_proc_resolve_record("lfa1_vendor_master", {"lifnr": step_proc_lifnr})
        if step_proc_vendor_rec: step_proc_vendor_name = step_proc_vendor_rec.get("name1", "Unknown Vendor")
        else: step_proc_final_output["exceptions"].append("VENDOR_UNKNOWN")

    # 4. Duplicate Screen
    step_proc_xblnr_raw = str(step_proc_raw_invoice.get("xblnr") or "").strip()
    step_proc_xblnr_norm = step_proc_xblnr_raw.upper() if step_proc_policy.get("normalization", {}).get("uppercase_reference_numbers") else step_proc_xblnr_raw
    
    # 5. Upsert ap_cases
    step_proc_status = "normalized" if not step_proc_final_output["exceptions"] else "needs_review"
    step_proc_upsert_payload = {
        "belnr": step_proc_belnr,
        "lifnr": step_proc_lifnr,
        "xblnr_norm": step_proc_xblnr_norm,
        "amount": step_proc_amount or 0,
        "wrbtr_raw": step_proc_wrbtr_raw,
        "vendor_name": step_proc_vendor_name,
        "status": step_proc_status,
        "run_id": step_proc_run_id,
        "exception_reasons": ";".join(sorted(list(set(step_proc_final_output["exceptions"])))),
        "updated_at": datetime.utcnow().isoformat()
    }

    res = step_proc_supabase.table("ap_cases").upsert(step_proc_upsert_payload, on_conflict="belnr").execute()
    caseId = res.data[0].get("case_id")
    
    # 6. Audit Log
    step_proc_supabase.table("ap_audit_log").insert({
        "case_id": caseId,
        "belnr": step_proc_belnr,
        "actor": "AP-02",
        "run_id": step_proc_run_id,
        "event": "NORMALIZED",
        "detail": f"Exceptions: {', '.join(step_proc_final_output['exceptions'])}."
    }).execute()

    print(f"Audit log entry created for Case: {caseId}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(step_process_invoice_execution())
