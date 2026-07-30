import logging
import re
from app.models.enums import DecisionType
from app.core.config import settings

logger = logging.getLogger(__name__)


def _make_renamed_rule(rule: str, unavailable_rules: set) -> str | None:
    """
    Collision-safe PRR rule rename that PRESERVES prefix and suffix so the
    renamed rule stays in scope (PRR scope = prefix AND suffix match):
        ild_dte_s6a → ild_dte2_s6a → ild_dte3_s6a …
    The counter is inserted before the final '_' segment; appending it at
    the end would break the suffix rule (…_s6a2 no longer ends with s6a).
    """
    max_len = settings.PRR_NAME_MAX_LEN

    if "_" in rule:
        prefix, suffix = rule.rsplit("_", 1)
    else:
        prefix, suffix = rule, ""

    i = 2
    while True:
        candidate = f"{prefix}{i}_{suffix}" if suffix else f"{prefix}{i}"

        if len(candidate) > max_len:
            logger.error("PRR Rename failed: candidate '%s' exceeds max length %d", candidate, max_len)
            return None

        if candidate.lower() not in unavailable_rules:
            return candidate
        i += 1


async def evaluate_prr_instance(ctx, row: dict, request_id: str) -> dict:
    realm = (row.get("Realm") or "").strip().lower()
    rule = (row.get("PRT Rule") or "").strip().lower()
    action = (row.get("ACTION") or "ADD").upper()

    if action not in {"ADD", "DELETE"}:
        return {"decision": DecisionType.SKIPPED.value, "reason": f"Invalid action: {action}"}

    if not realm:
        return {"decision": DecisionType.SKIPPED.value, "reason": "Missing mandatory Realm parameter"}
    
    if action == "ADD" and not rule:
        return {"decision": DecisionType.SKIPPED.value, "reason": "Missing mandatory PRT Rule parameter"}

    # ──────────────────────────────────────────────────────────────
    # 1. Read Effective State Directly From Pre-Hydrated Context
    # ──────────────────────────────────────────────────────────────
    # Because context.py already replayed pending deltas into these sets,
    # they represent the exact state the DB will be in after the current queue.
    effective_realm_exists = realm in ctx.prr_realms
    all_unavailable_rules = ctx.prr_rules

    # ──────────────────────────────────────────────────────────────
    # 2. Collect All Chained Relationship Dependencies
    # ──────────────────────────────────────────────────────────────
    dependency_add_relationships = []
    dependency_delete_relationships = []
    
    pending_items = ctx.prr_pending_realms.get(realm, [])

    for item in pending_items:
        p_action = item["action"]
        dep_req = item["request_id"]

        # If a prior step is deleting this realm, a new ADD must wait for it
        if p_action == "DELETE" and action == "ADD":
            dependency_add_relationships.append({
                "request_id": dep_req,
                "type": DecisionType.DEPENDENCY_ADD.value
            })

        # If a prior step is adding this realm, a new DELETE must wait for it
        elif p_action == "ADD" and action == "DELETE":
            dependency_delete_relationships.append({
                "request_id": dep_req,
                "type": DecisionType.DEPENDENCY_DELETE.value
            })

    # ──────────────────────────────────────────────────────────────
    # 3. Evaluate Action Against Effective State & Rules
    # ──────────────────────────────────────────────────────────────
    if action == "ADD":
        # Check for absolute duplicates based on the net-effective state
        if effective_realm_exists:
            return {
                "decision": DecisionType.SKIPPED.value,
                "reason": f"Duplicate ADD: Realm '{realm}' will already exist via snapshot or pending pipeline.",
            }

        # Resolve naming collisions safely using our fixed lookup constraints
        final_rule = rule
        if rule in all_unavailable_rules:
            final_rule = _make_renamed_rule(rule, all_unavailable_rules)
            if final_rule is None:
                return {
                    "decision": DecisionType.SKIPPED.value,
                    "reason": f"Name conflict: rename exceeds {settings.PRR_NAME_MAX_LEN} chars.",
                }

        # If there are active dependencies in flight, pass the full array
        if dependency_add_relationships:
            return {
                "decision": DecisionType.DEPENDENCY_ADD.value,
                "final_rule": final_rule.lower(),
                "dependency_note": "Dependent on pending pipeline deletions.",
                "relationships": dependency_add_relationships,
            }

        return {"decision": DecisionType.ADD.value, "final_rule": final_rule.lower()}

    elif action == "DELETE":
        if not effective_realm_exists:
            return {
                "decision": DecisionType.SKIPPED.value,
                "reason": f"Duplicate DELETE: Realm '{realm}' does not exist or is already deleted.",
            }

        if dependency_delete_relationships:
            return {
                "decision": DecisionType.DEPENDENCY_DELETE.value,
                "dependency_note": "Dependent on pending pipeline additions.",
                "relationships": dependency_delete_relationships,
            }

        return {"decision": DecisionType.DELETE.value}