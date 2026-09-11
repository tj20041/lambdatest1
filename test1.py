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


class OrderValidationError(Exception):
    """Raised when order payload data fails business/type validation
    (e.g. a non-numeric unit_price). This is distinct from unexpected
    runtime exceptions so the Lambda handler can return a clean 4xx
    response instead of failing the entire invocation."""
    pass


def _coerce_to_float(value: Any, field_name: str) -> float:
    """Safely coerce a numeric-like value (int, float, or numeric string)
    into a float. Raises OrderValidationError on any non-numeric input
    so callers can surface a clean validation error instead of a raw
    TypeError/ValueError."""
    if isinstance(value, bool):
        # bool is technically an int subclass in Python; explicitly reject it
        raise OrderValidationError(f"Field '{field_name}' must be numeric, got boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, AttributeError):
            raise OrderValidationError(f"Field '{field_name}' has non-numeric value: '{value}'")
    raise OrderValidationError(f"Field '{field_name}' has unsupported type: {type(value).__name__}")


def _is_valid_price(value: Any) -> bool:
    """Returns True if value is numeric or a numeric-parseable string
    representing a non-negative price. Used during payload validation
    to reject malformed line items before they reach the arithmetic
    engine in process_order_calculations."""
    try:
        parsed = _coerce_to_float(value, "unit_price")
    except OrderValidationError:
        return False
    return parsed >= 0


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
        if not isinstance(item["quantity"], int) or isinstance(item["quantity"], bool) or item["quantity"] <= 0:
            return False, f"Line item at index {idx} has invalid quantity"
        if not _is_valid_price(item["unit_price"]):
            return False, f"Line item at index {idx} has invalid or non-numeric unit_price: '{item['unit_price']}'"
            
    return True, None

def process_order_calculations(order_data: Dict[str, Any], tracker: PaymentAuditTracker) -> Dict[str, Any]:
    region = order_data.get("billing_region", "DEFAULT")
    tax_multiplier = TAX_RATES.get(region, TAX_RATES["DEFAULT"])
    discount_code = order_data.get("discount_code")
    discount_rate = DISCOUNT_CODES.get(discount_code, 0.0)

    tracker.record_step("CALCULATION_INIT", {"region": region, "tax_multiplier": tax_multiplier})

    running_subtotal = 0.0
    line_item_breakdowns = []

    for item in order_data["line_items"]:
        sku = item["sku"]
        qty = item["quantity"]
        # Order payloads commonly arrive from API Gateway with numeric fields
        # serialized as JSON strings (e.g. "149.99"). Explicitly coerce to
        # float here so downstream arithmetic never operates on a str.
        unit_price = _coerce_to_float(item["unit_price"], "unit_price")

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

    # Self-contained simulated API Gateway event payload.
    # unit_price is intentionally kept as a JSON string here to mirror the
    # typical API Gateway payload shape; process_order_calculations now
    # safely coerces this to a numeric type before performing arithmetic.
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
        try:
            pricing_manifest = process_order_calculations(parsed_payload, tracker)
        except OrderValidationError as val_err:
            # Business/data validation failure (e.g. malformed numeric field
            # that slipped past validate_order_contract). Return a clean
            # 422 response instead of failing the entire Lambda invocation.
            logger.error(f"Order calculation validation failure: {str(val_err)}")
            return {
                "statusCode": 422,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "error": str(val_err),
                    "request_id": request_id,
                    "order_id": parsed_payload.get("order_id")
                })
            }

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

    except (TypeError, ValueError, KeyError) as known_exc:
        # Known, recoverable data-shape issues: log and return a structured
        # 400 response so API Gateway callers get a well-formed error body
        # instead of the Lambda invocation itself failing.
        logger.exception(f"Recoverable data error in order execution pipeline: {str(known_exc)}")
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(known_exc), "request_id": request_id})
        }
    except Exception as exc:
        # Truly unexpected exceptions are still logged and re-raised so
        # Lambda/CloudWatch error metrics and alarms continue to fire.
        logger.exception(f"Unhandled error in order execution pipeline: {str(exc)}")
        raise
