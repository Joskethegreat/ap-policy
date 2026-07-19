import os
import json
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from IPython.display import HTML, display
from supabase import create_client, Client

async def process_invoice_logic():
    # Namespaced variables
    step_proc_belnr = user_inputs.get("belnr")
    step_proc_input_run_id = user_inputs.get("run_id")
    
    print(f"Processing Invoice Document Number (belnr): {step_proc_belnr}")
    
    step_proc_final_output = { "ok": True, "exceptions": [], "notes": [] }

    if not step_proc_belnr:
        step_proc_final_output["ok"] = False
        step_proc_final_output["exceptions"].append("MISSING_REQUIRED_FIELD")
        print(json.dumps(step_proc_final_output, indent=2))
        return

    step_proc_run_id = step_proc_input_run_id if step_proc_input_run_id else str(uuid.uuid4())
    step_proc_policy = globals().get('policy', {})
    
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY")
    
    try:
        supabase: Client = create_client(sb_url, sb_key)

        def resolve_record(table, filters):
            res = supabase.table(table).select("*")
            for k, v in filters.items(): res = res.eq(k, v)
            data = res.execute().data
            if not data: return None
            if len(data) == 1: return data[0]
            
            # Tie breaker logic
            ref_cfg = step_proc_policy.get("reference_data", {})
            tie_breaker = ref_cfg.get("tie_breaker", "latest_erdat")
            if tie_breaker == "latest_erdat":
                return sorted(data, key=lambda x: str(x.get("erdat") or x.get("created_at") or ""), reverse=True)[0]
            return data[0]

        # 1. Lookup
        raw_inv = resolve_record("rbkp_invoice", {"belnr": step_proc_belnr})
        if not raw_inv:
            step_proc_final_output["ok"] = False
            step_proc_final_output["exceptions"].append("MISSING_REQUIRED_FIELD")
            return

        # 2. Normalize Amount
        raw_wrbtr = str(raw_inv.get("wrbtr") or "").strip()
        def parse_amt(s):
            if not s: return None
            s = s.replace(" ", "")
            try:
                if "," in s and "." in s: return float(s.replace(",", ""))
                if "," in s: return float(s.replace(",", "."))
                return float(s)
            except: return None

        amt = parse_amt(raw_wrbtr)
        if amt is None: step_proc_final_output["exceptions"].append("UNPARSEABLE_VALUE")

        # 3. Vendor Check
        lifnr = str(raw_inv.get("lifnr") or "").strip()
        v_name = "Unknown Vendor"
        if lifnr:
            v_rec = resolve_record("lfa1_vendor_master", {"lifnr": lifnr})
            if v_rec: v_name = v_rec.get("name1", v_name)
            else: step_proc_final_output["exceptions"].append("VENDOR_UNKNOWN")

        # 4. Duplicate Screen
        xblnr = str(raw_inv.get("xblnr") or "").strip()
        xblnr_norm = xblnr.upper() if step_proc_policy.get("normalization", {}).get("uppercase_reference_numbers") else xblnr
        
        # 5. Upsert ap_cases
        status = "normalized" if not step_proc_final_output["exceptions"] else "needs_review"
        payload = {
            "belnr": step_proc_belnr,
            "lifnr": lifnr,
            "xblnr_norm": xblnr_norm,
            "amount": amt or 0,
            "wrbtr_raw": raw_wrbtr,
            "vendor_name": v_name,
            "status": status,
            "run_id": step_proc_run_id,
            "exception_reasons": ";".join(sorted(list(set(step_proc_final_output["exceptions"])))),
            "updated_at": datetime.utcnow().isoformat()
        }

        res = supabase.table("ap_cases").upsert(payload, on_conflict="belnr").execute()
        case_id = res.data[0].get("case_id")

        # 6. Audit Log
        detail = f"Exceptions: {', '.join(step_proc_final_output['exceptions'])}. Notes: {', '.join(step_proc_final_output['notes'])}."
        supabase.table("ap_audit_log").insert({
            "case_id": case_id,
            "belnr": step_proc_belnr,
            "actor": "AP-02",
            "run_id": step_proc_run_id,
            "event": "NORMALIZED",
            "detail": detail
        }).execute()

        print(f"Processed case {case_id} for belnr {step_proc_belnr}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        step_proc_final_output["ok"] = False
        raise

    print(json.dumps(step_proc_final_output, indent=2))
