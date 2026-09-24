import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Structured Enterprise Logging Setup
# ---------------------------------------------------------------------------
class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "source_location": f"{record.filename}:{record.lineno}",
        }
        if hasattr(record, "custom_dimensions"):
            log_entry["dimensions"] = record.custom_dimensions
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

logger = logging.getLogger("payment_state_machine")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(StructuredJsonFormatter())
logger.handlers = [handler]

# ---------------------------------------------------------------------------
# Business Domain Rules & Engine
# ---------------------------------------------------------------------------
TAX_RATES = {
    "US-CA": 0.0925,
    "US-NY": 0.08875,
    "US-TX": 0.0825,
    "EU-DE": 0.1900,
    "DEFAULT": 0.0500
}

DISCOUNT_CODES = {
    "WELCOME10": 0.10,
    "VIP20": 0.20,
    "FREESHIP": 0.0
}

class PaymentAuditTracker:
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.events: List[Dict[str, Any]] = []

    def record_step(self, stage_name: str, payload_summary: Dict[str, Any]) -> None:
        event = {
            "stage": stage_name,
            "trace_id": self.trace_id,
            "epoch_ms": int(time.time() * 1000),
            "summary": payload_summary
        }
        self.events.append(event)
        logger.info(f"Audit Step Recorded: {stage_name}", extra={"custom_dimensions": event})

def verify_hmac_signature(secret: str, payload: str, signature: str) -> bool:
    computed = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)

def validate_order_contract(payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    required_fields = ["order_id", "customer_id", "billing_region", "line_items"]
    for field in required_fields:
        if field not in payload:
            return False, f"Missing required payload attribute: '{field}'"

    if not isinstance(payload["line_items"], list) or len(payload["line_items"]) == 0:
        return False, "line_items must be a non-empty list"

    for idx, item in enumerate(payload["line_items"]):
        if not all(k in item for k in ("sku", "quantity", "unit_price")):
            return False, f"Line item at index {idx} violates schema: {item}"
        if not isinstance(item["quantity"], int) or item["quantity"] <= 0:
            return False, f"Line item at index {idx} has invalid quantity"
        # Validate that unit_price is numeric or a string convertible to a positive float
        try:
            price_val = float(item["unit_price"])
            if price_val < 0:
                return False, f"Line item at index {idx} has negative unit_price"
        except (TypeError, ValueError):
            return False, f"Line item at index {idx} has non-numeric unit_price: {item['unit_price']!r}"

    return True, None


class OrderCalculationError(Exception):
    """Raised when a line item contains a unit_price that cannot be coerced to float."""
    pass


def process_order_calculations(order_data: Dict[str, Any], tracker: PaymentAuditTracker) -> Dict[str, Any]:
    region = order_data.get("billing_region", "DEFAULT")
    tax_multiplier = TAX_RATES.get(region, TAX_RATES["DEFAULT"])
    discount_code = order_data.get("discount_code")
    discount_rate = DISCOUNT_CODES.get(discount_code, 0.0)

    tracker.record_step("CALCULATION_INIT", {"region": region, "tax_multiplier": tax_multiplier})

    running_subtotal = 0.0
    line_item_breakdowns = []

    for idx, item in enumerate(order_data["line_items"]):
        sku = item["sku"]
        qty = item["quantity"]
        # Coerce unit_price to float to handle string values from API ingestion
        # (e.g. "149.99") as well as already-numeric values.
        try:
            unit_price = float(item["unit_price"])
        except (TypeError, ValueError) as exc:
            raise OrderCalculationError(
                f"Line item at index {idx} (sku={sku!r}) has non-numeric unit_price: "
                f"{item['unit_price']!r}"
            ) from exc

        item_tax = unit_price * tax_multiplier
        extended_price = unit_price * qty

        line_item_breakdowns.append({
            "sku": sku,
            "quantity": qty,
            "unit_price": unit_price,
            "calculated_tax": round(item_tax, 2),
            "extended_price": round(extended_price, 2)
        })
        running_subtotal += extended_price

    discount_amount = running_subtotal * discount_rate
    final_tax = running_subtotal * tax_multiplier
    grand_total = (running_subtotal - discount_amount) + final_tax

    result = {
        "subtotal": round(running_subtotal, 2),
        "discount_applied": round(discount_amount, 2),
        "total_tax": round(final_tax, 2),
        "grand_total": round(grand_total, 2),
        "items": line_item_breakdowns
    }

    tracker.record_step("CALCULATION_COMPLETE", {"grand_total": result["grand_total"]})
    return result

# ---------------------------------------------------------------------------
# Lambda Handler Entrypoint
# ---------------------------------------------------------------------------
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    tracker = PaymentAuditTracker(trace_id=request_id)

    logger.info("Initializing payment checkout order flow", extra={"custom_dimensions": {"request_id": request_id}})

    # Self-contained simulated API Gateway event payload
    synthetic_event = {
        "headers": {
            "x-signature": "5d41402abc4b2a76b9719d911017c592",
            "content-type": "application/json"
        },
        "body": json.dumps({
            "order_id": "ORD-2026-9981",
            "customer_id": "CUST-88120",
            "billing_region": "US-CA",
            "discount_code": "WELCOME10",
            "line_items": [
                {"sku": "LAPTOP-M3", "quantity": 1, "unit_price": "1299.99"},
                {"sku": "USB-C-DOCK", "quantity": 2, "unit_price": "89.50"}
            ]
        })
    }

    try:
        body_str = synthetic_event.get("body", "{}")
        parsed_payload = json.loads(body_str)

        is_valid, validation_err = validate_order_contract(parsed_payload)
        if not is_valid:
            logger.error(f"Payload validation rejection: {validation_err}")
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": validation_err, "request_id": request_id})
            }

        tracker.record_step("VALIDATION_SUCCESS", {"order_id": parsed_payload["order_id"]})

        # Trigger business processing logic
        pricing_manifest = process_order_calculations(parsed_payload, tracker)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "status": "APPROVED",
                "order_id": parsed_payload["order_id"],
                "pricing": pricing_manifest,
                "audit_trail": tracker.events
            })
        }

    except OrderCalculationError as calc_err:
        # Data-quality error caused by the caller — return a structured 400
        logger.error(
            f"Order calculation rejected due to invalid input data: {str(calc_err)}",
            extra={"custom_dimensions": {"request_id": request_id}}
        )
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(calc_err), "request_id": request_id})
        }

    except Exception as exc:
        # Genuine unexpected server error — log with full traceback and return 500
        logger.exception(
            f"Unhandled internal server error in order execution pipeline: {str(exc)}",
            extra={"custom_dimensions": {"request_id": request_id}}
        )
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "error": "Internal server error",
                "request_id": request_id
            })
        }
