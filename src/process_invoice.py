import os
import json
import sys
import uuid
import base64
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from IPython.display import HTML, display
from supabase import create_client, Client

async def step_process_invoice_run():
    # 1. Namespaced variables
    step_id = "process_invoice"
    step_proc_belnr = user_inputs.get("belnr")
    step_proc_input_run_id = user_inputs.get("run_id")
    
    # Final Output JSON structure as requested
    step_proc_final_output = {
        "ok": True,
        "exceptions": [],
        "notes": []
    }

    # Helper: Generate or use Run ID
    step_proc_run_id = step_proc_input_run_id if step_proc_input_run_id and str(step_proc_input_run_id).strip() else str(uuid.uuid4())

    # 2. Policy extraction from previous step (globals)
    step_proc_policy = globals().get('policy', {})
    
    # 3. Supabase Initialization
    step_proc_sb_url = os.environ.get("SUPABASE_URL")
    step_proc_sb_key = os.environ.get("SUPABASE_SERVICE_KEY")
    
    # Auto-fix Supabase URL if it's a token
    if step_proc_sb_url and not step_proc_sb_url.startswith("http"):
        try:
            parts = step_proc_sb_url.split('.')
            if len(parts) == 3:
                payload_b64 = parts[1] + '=' * (-len(parts[1]) % 4)
                payload_json = base64.decodebytes(payload_b64.encode()).decode('utf-8')
                project_ref = json.loads(payload_json).get("ref")
                if project_ref:
                    step_proc_sb_url = f"https://{project_ref}.supabase.co"
        except Exception:
            pass

    try:
        if not step_proc_sb_url or not step_proc_sb_key:
            raise ValueError("Supabase credentials missing.")

        step_proc_supabase: Client = create_client(step_proc_sb_url, step_proc_sb_key)

        # 4. Helper: Reference Lookup Rule
        def step_proc_resolve_record(table_name: str, filters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            ref_cfg = step_proc_policy.get("reference_data", {})
            
            query = step_proc_supabase.table(table_name).select("*")
            for k, v in filters.items():
                query = query.eq(k, v)
            
            response = query.execute()
            data = response.data
            
            if not data:
                return None
            if len(data) == 1:
                return data[0]
            
            # Multiple rows logic
            ignore_fields = [f.lower().strip() for f in ref_cfg.get("ignore_fields_when_comparing", [])]
            
            def get_comparable(rec):
                return {k.lower(): v for k, v in rec.items() if k.lower() not in ignore_fields}
            
            base_row = data[0]
            base_comp = get_comparable(base_row)
            all_identical = all(get_comparable(r) == base_comp for r in data[1:])
            
            if all_identical:
                action = ref_cfg.get("identical_rows_action", "use_first")
                step_proc_final_output["notes"].append(f"Identical rows in {table_name}; applied {action}.")
                return base_row
            
            # Tie breaker
            tie_breaker = ref_cfg.get("tie_breaker", "latest_erdat")
            if tie_breaker == "latest_erdat":
                # Look for common erdat or created_at fields
                sorted_data = sorted(data, key=lambda x: str(x.get("erdat") or x.get("created_at") or ""), reverse=True)
                selected = sorted_data[0]
            elif tie_breaker == "lowest_id":
                sorted_data = sorted(data, key=lambda x: str(x.get("id") or x.get("belnr") or ""), reverse=False)
                selected = sorted_data[0]
            else:
                selected = data[0]
            
            step_proc_final_output["notes"].append(f"Conflict in {table_name}; tie-breaker {tie_breaker} applied.")
            return selected

        # 5. Execution Steps
        # Lookup rbkp_invoice
        if not step_proc_belnr:
            step_proc_final_output["ok"] = False
            step_proc_final_output["exceptions"].append("MISSING_REQUIRED_FIELD")
        else:
            step_proc_raw_invoice = step_proc_resolve_record("rbkp_invoice", {"belnr": step_proc_belnr})
            if not step_proc_raw_invoice:
                step_proc_final_output["ok"] = False
                step_proc_final_output["exceptions"].append("MISSING_REQUIRED_FIELD")
            else:
                # Normalization: Amount Parsing (Stricter)
                step_proc_wrbtr_raw = str(step_proc_raw_invoice.get("wrbtr") or "").strip()
                step_proc_amount = None
                
                def step_proc_parse_amount(val_str: str) -> Optional[float]:
                    if not val_str: return None
                    norm_cfg = step_proc_policy.get("normalization", {})
                    euro_comma = norm_cfg.get("amount_formats", {}).get("european_decimal_comma", False)
                    
                    # Remove whitespace
                    s = val_str.replace(" ", "")
                    has_dot = "." in s
                    has_comma = "," in s
                    
                    try:
                        # Policy requirement: If dot and no comma -> parse as float
                        if has_dot and not has_comma:
                            return float(s)
                        
                        # Policy requirement: If comma and no dot, OR euro_comma is active
                        if (has_comma and not has_dot) or (has_comma and euro_comma):
                            # European format: 1.234,56 -> 1234.56
                            # Or comma decimal: 123,45 -> 123.45
                            clean = s.replace(".", "") # thousands
                            clean = clean.replace(",", ".") # decimal
                            return float(clean)
                        
                        # US format with thousands: 1,234.56
                        if has_dot and has_comma:
                            clean = s.replace(",", "")
                            return float(clean)
                        
                        return float(s)
                    except (ValueError, TypeError):
                        return None

                step_proc_amount = step_proc_parse_amount(step_proc_wrbtr_raw)
                if step_proc_amount is None:
                    step_proc_final_output["exceptions"].append("UNPARSEABLE_VALUE")
                
                # PO Number
                step_proc_ebeln = str(step_proc_raw_invoice.get("ebeln") or "").strip()
                
                # Vendor Check
                step_proc_lifnr = str(step_proc_raw_invoice.get("lifnr") or "").strip()
                step_proc_vendor_name = "Unknown Vendor"
                if step_proc_lifnr:
                    step_proc_vendor_rec = step_proc_resolve_record("lfa1_vendor_master", {"lifnr": step_proc_lifnr})
                    if not step_proc_vendor_rec:
                        step_proc_final_output["exceptions"].append("VENDOR_UNKNOWN")
                    else:
                        step_proc_vendor_name = step_proc_vendor_rec.get("name1", "Unknown Vendor")

                # Duplicate Screen
                step_proc_xblnr_raw = str(step_proc_raw_invoice.get("xblnr") or "").strip()
                norm_cfg = step_proc_policy.get("normalization", {})
                step_proc_xblnr_norm = step_proc_xblnr_raw.upper() if norm_cfg.get("uppercase_reference_numbers") else step_proc_xblnr_raw
                
                if step_proc_lifnr and step_proc_xblnr_norm:
                    dup_cfg = step_proc_policy.get("duplicates", {})
                    lookback = dup_cfg.get("lookback_days", 365)
                    cutoff = (datetime.utcnow() - timedelta(days=lookback)).isoformat()
                    
                    dup_query = step_proc_supabase.table("ap_cases").select("*") \
                        .eq("lifnr", step_proc_lifnr) \
                        .eq("xblnr_norm", step_proc_xblnr_norm) \
                        .neq("status", "rejected") \
                        .neq("belnr", step_proc_belnr) \
                        .gte("created_at", cutoff)
                    
                    dup_res = dup_query.execute()
                    
                    if dup_res.data:
                        for existing in dup_res.data:
                            existing_amt = float(existing.get("amount") or 0)
                            if step_proc_amount is not None and abs(existing_amt - step_proc_amount) < 0.01:
                                step_proc_final_output["exceptions"].append("DUPLICATE_INVOICE")
                                break
                            elif dup_cfg.get("also_flag_same_reference_different_amount"):
                                step_proc_final_output["exceptions"].append("DUPLICATE_INVOICE")
                                break

                # Upsert ap_cases
                step_proc_status = "normalized" if not step_proc_final_output["exceptions"] else "needs_review"
                
                step_proc_upsert_payload = {
                    "belnr": step_proc_belnr,
                    "lifnr": step_proc_lifnr,
                    "xblnr_norm": step_proc_xblnr_norm,
                    "amount": step_proc_amount if step_proc_amount is not None else 0,
                    "wrbtr_raw": step_proc_wrbtr_raw,
                    "ebeln": step_proc_ebeln,
                    "waers": str(step_proc_raw_invoice.get("waers") or "").strip(),
                    "vendor_name": step_proc_vendor_name,
                    "invoice_type": "PO" if (step_proc_ebeln and step_proc_ebeln.lower() != "none") else "NON_PO",
                    "status": step_proc_status,
                    "run_id": step_proc_run_id,
                    "exception_reasons": ";".join(sorted(list(set(step_proc_final_output["exceptions"])))),
                    "notes": "; ".join(step_proc_final_output["notes"]),
                    "updated_at": datetime.utcnow().isoformat()
                }

                step_proc_db_res = step_proc_supabase.table("ap_cases").upsert(step_proc_upsert_payload, on_conflict="belnr").execute()
                
                if not step_proc_db_res.data:
                    step_proc_final_output["exceptions"].append("DATABASE_WRITE_FAILED")
                    step_proc_final_output["ok"] = False
                else:
                    # Audit Log: Use actual integer case_id (or 'id') from response
                    step_proc_case_id_int = step_proc_db_res.data[0].get("case_id") or step_proc_db_res.data[0].get("id")
                    
                    step_proc_audit_payload = {
                        "case_id": step_proc_case_id_int,
                        "belnr": step_proc_belnr,
                        "actor": "AP-02",
                        "event": "NORMALIZED",
                        "run_id": step_proc_run_id,
                        "created_at": datetime.utcnow().isoformat()
                    }
                    step_proc_supabase.table("ap_audit_log").insert(step_proc_audit_payload).execute()

    except Exception as e:
        import traceback
        print(f"Error in process_invoice: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        step_proc_final_output["ok"] = False
        if "OPERATOR_FAILED_AFTER_RETRY" not in step_proc_final_output["exceptions"]:
            step_proc_final_output["exceptions"].append("OPERATOR_FAILED_AFTER_RETRY")

    # 6. Final UI/Output: "Just indicate execution completion status"
    status_badge = "ag-badge-success" if step_proc_final_output["ok"] and not step_proc_final_output["exceptions"] else "ag-badge-warning"
    if not step_proc_final_output["ok"]:
        status_badge = "ag-badge-error"

    html_out = f"""
    <div class="ag-root">
        <div class="ag-card">
            <h2 class="ag-h2">Invoice Validation & Normalization</h2>
            <p class="ag-body">The workflow processing for the invoice document has completed.</p>
            <table class="ag-table">
                <thead>
                    <tr><th>Parameter</th><th>Status</th></tr>
                </thead>
                <tbody>
                    <tr><td>Workflow Execution</td><td><span class="ag-badge {status_badge}">{"Completed" if step_proc_final_output['ok'] else "Failed"}</span></td></tr>
                </tbody>
            </table>
        </div>
    </div>
    """
    display(HTML(html_out))
    print(json.dumps(step_proc_final_output, indent=2, default=str))

# Run the step
await step_process_invoice_run()