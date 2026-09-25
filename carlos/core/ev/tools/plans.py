"""Model-composed typed plans share the existing executor, not a shell runner."""

import asyncio
import copy
import json
import re
import uuid

from ..execution import execution_correlation
from ..goals import validate_conditions, verify_conditions
from ..permissions import Permission
from ..planner import TaskPlan
from .base import ToolSpec, ValidationError, validate_schema
from .builtin import object_schema


def build_plan(arguments, planner, correlation):
    """Validate structure before effects; resolve typed result references at runtime."""
    steps, groups, seen, mutations = [], [], set(), set()

    def validate_template(value, schema, path="arguments"):
        if isinstance(value, dict) and set(value) == {"$ref"}:
            return  # Value's actual type is checked after the reference resolves.
        if isinstance(value, dict) and schema.get("type") == "object":
            properties = schema.get("properties", {})
            missing = set(schema.get("required", [])) - set(value)
            unknown = (
                set(value) - set(properties)
                if schema.get("additionalProperties") is False
                else set()
            )
            if missing or unknown:
                raise ValidationError(
                    f"{path}: missing required fields {sorted(missing)}; unknown fields {sorted(unknown)}. Use the registered tool argument names."
                )
            for key, item in value.items():
                validate_template(item, properties.get(key, {}), f"{path}.{key}")
        elif isinstance(value, list) and schema.get("type") == "array":
            if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
                raise ValidationError("Referenced argument array is out of bounds")
            for index, item in enumerate(value):
                validate_template(item, schema.get("items", {}), f"{path}[{index}]")
        else:
            validate_schema(value, schema, path)

    def refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                if (
                    set(value) != {"$ref"}
                    or not isinstance(value["$ref"], str)
                    or value["$ref"].split(".")[0] not in seen
                ):
                    raise ValidationError("References must identify a prior step result")
                return True
            return any([refs(v) for v in value.values()])
        if isinstance(value, list):
            return any([refs(v) for v in value])
        return False

    def condition_template(conditions):
        check = copy.deepcopy(conditions)
        if not isinstance(check, list):
            raise ValidationError("Conditions must be a list")
        for condition in check:
            if not isinstance(condition, dict):
                raise ValidationError("Invalid condition")
            if condition.get("kind") in {"control_text", "control_state"}:
                from .controls import CONTROL_IDENTITY_SCHEMA

                placeholders = {
                    key: 1 if schema.get("type") == "integer" else "resolved-target"
                    for key, schema in CONTROL_IDENTITY_SCHEMA["properties"].items()
                }
                target = condition.get("target")
                if isinstance(target, dict) and "$ref" in target:
                    refs(target)
                    condition["target"] = placeholders
                elif isinstance(target, dict):
                    for field, placeholder in placeholders.items():
                        if isinstance(target.get(field), dict):
                            if set(target[field]) != {"$ref"}:
                                raise ValidationError(
                                    "Control identities must be exact values or prior-step references"
                                )
                            refs(target[field])
                            target[field] = placeholder
            identities = {"window_id": "resolved-target", "path": "resolved-target"}
            if condition.get("kind") == "media_playback":
                # Unique owners are runtime observations, not values a model
                # should guess before submitting a plan. Only identity fields
                # may reference receipts; the desired state remains literal.
                identities.update(service="org.mpris.MediaPlayer2.resolved", owner=":1.1")
            for field, placeholder in identities.items():
                if isinstance(condition.get(field), dict):
                    if set(condition[field]) != {"$ref"}:
                        raise ValidationError(
                            "Condition identities must be exact values or prior-step references"
                        )
                    refs(condition[field])
                    condition[field] = placeholder
        validate_conditions(check)

    def visit(items, group="", depth=0):
        if not isinstance(items, list) or not items or depth > 3:
            raise ValidationError("Groups must be nonempty and at most four levels deep")
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError("Plan entries must be objects")
            if "steps" in item:
                if (
                    set(item) != {"group", "steps"}
                    or not isinstance(item["group"], str)
                    or not 1 <= len(item["group"]) <= 100
                ):
                    raise ValidationError("Invalid plan group")
                group_path = group + "/" + item["group"]
                groups.append(group_path)
                if len(groups) > 12:
                    raise ValidationError("Too many plan groups")
                visit(item["steps"], group_path, depth + 1)
                continue
            if set(item) - {"id", "tool", "arguments", "repeat_intentionally"} or not {
                "id",
                "tool",
                "arguments",
            } <= set(item):
                raise ValidationError("Step has missing or unsupported fields")
            identifier = item["id"]
            if (
                not isinstance(identifier, str)
                or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_]{0,39}", identifier)
                or identifier in seen
            ):
                raise ValidationError(
                    "Step IDs must be unique strings of 1-40 letters, digits or underscores, beginning with a letter or digit"
                )
            if len(steps) >= 32:
                raise ValidationError("Plan exceeds 32 actions")
            spec = planner.registry.get(item["tool"])
            observation_coordinator = (
                spec.name in {"agent.wait_for", "agent.verify_conditions"}
                and spec.read_only
                and spec.permission == Permission.SAFE
            )
            if (
                (spec.name.startswith("agent.") and not observation_coordinator)
                or spec.name in {"system.power", "security.firewall.runtime"}
                or spec.requires_confirmation
            ):
                raise ValidationError(
                    "Nested plans, privileged/session-ending and confirmation-gated actions require separate explicit requests"
                )
            if not isinstance(item["arguments"], dict):
                raise ValidationError("Step arguments must be an object")
            references = refs(item["arguments"])
            validate_template(
                item["arguments"], spec.schema, f"Step {identifier} ({spec.name}) arguments"
            )
            if not references:
                planner.registry.validate(spec.name, item["arguments"])
            if observation_coordinator:
                # These coordinators only call finite read-only predicates via
                # the existing planner-owned executor, without reacquiring the
                # planner lock. Validate predicates BEFORE any preceding effect.
                condition_template(item["arguments"]["conditions"])
            signature = json.dumps([spec.name, item["arguments"]], sort_keys=True)
            if (
                not spec.read_only
                and signature in mutations
                and item.get("repeat_intentionally") is not True
            ):
                raise ValidationError(
                    "Duplicate action; observe first or explicitly declare intentional repetition"
                )
            if not spec.read_only:
                mutations.add(signature)
            dependencies = [steps[-1].id] if steps else []
            steps.append(
                planner._step(
                    identifier,
                    spec.name,
                    copy.deepcopy(item["arguments"]),
                    group or spec.verification,
                    dependencies,
                )
            )
            seen.add(identifier)

    visit(arguments["steps"])
    conditions = copy.deepcopy(arguments["conditions"])
    condition_template(conditions)
    plan = TaskPlan(uuid.uuid4().hex, correlation, arguments["goal"], arguments["goal"], steps)
    plan.goal_conditions, plan.goal_source = conditions, "model_declared"
    return plan


def register_plan_tools(registry, planner, requester):
    async def execute(arguments, context):
        correlation = execution_correlation.get() or uuid.uuid4().hex
        try:
            plan = build_plan(arguments, planner, correlation)
        except (ValidationError, TypeError, KeyError) as error:
            # Whole-plan preflight occurs before any child action. Distinguish
            # this from interruption, where already-sent effects are uncertain.
            return {
                "ok": False,
                "verified": False,
                "executed": False,
                "error_code": "plan_validation_failed",
                "error": str(error),
            }
        try:
            result = await asyncio.wait_for(
                planner.execute(plan, requester=requester, own_confirmation=False),
                timeout=arguments.get("timeout_seconds", 120),
            )
        except (asyncio.CancelledError, TimeoutError) as error:
            plan.cancellation_reason = "timeout" if isinstance(error, TimeoutError) else "cancelled"
            plan.status = "INTERRUPTED_UNCERTAIN"
            planner._archive(plan)
            planner._settle_terminal_state("Plan interrupted", correlation)
            if isinstance(error, asyncio.CancelledError):
                raise
            return {
                "ok": False,
                "error": "Plan time budget reached; an already-sent action may have taken effect",
                "plan_id": plan.id,
            }
        except Exception as error:
            plan.status = "FAILED"
            planner._archive(plan)
            planner._settle_terminal_state("Plan failed", correlation)
            return {"ok": False, "error": str(error), "plan_id": plan.id}
        verified = (
            result.get("status") == "completed"
            and result.get("declared_conditions_verified") is True
        )
        return {
            "ok": verified,
            "verified": verified,
            "scope": "declared_conditions",
            "plan_id": plan.id,
            "goal_verified": False,
            "goal_verification": plan.goal_verification,
            "cancelled": result.get("status") == "cancelled",
            "steps": [
                {"id": s.id, "tool": s.tool, "status": s.status, "attempts": s.attempts}
                for s in plan.steps
            ],
            **(
                {}
                if verified
                else {"error": result.get("response", "Declared conditions not satisfied")}
            ),
        }

    async def verify(arguments, context):
        return await verify_conditions(
            arguments["conditions"], requester, execution_correlation.get()
        )

    conditions = {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "object"}}
    description = "Conditions are exact objects: kind window_exists/window_absent/window_active + window_id; window_state + window_id/property(minimized,fullscreen,maximized)/expected boolean; window_geometry + window_id/geometry{x,y,width,height}; file_text + path/expected(exact UTF-8 text, including newlines); file_hash + path/sha256; file_kind + path/expected(file,directory); audio_volume/audio_muted + expected; control_state + target{window_id,process_id,window_title,path,name,role,application}/property(checked,selected,value)/expected(boolean or numeric value); control_text + target(same full control identity)/expected(exact complete text, at most 2000 characters); process_ended + pid/start_ticks/boot_id (obtain all three from system.process_lifetime; disappearance is not successful task completion). Every condition is freshly observed. Use file_text for requested text; do not invent a SHA-256 digest."
    description += " browser_url + window_id/expected(exact HTTP(S) URL) requires a fresh visible top-level browser document URL, unchanged focus and non-busy state; it does not certify page content or login state."
    description += " media_playback + service(exact observed MPRIS bus name)/owner(unique bus owner)/expected(Playing,Paused,Stopped) verifies that same player instance's fresh transport state, not track identity or song completion."
    description += ' Inside composed plans, condition window_id/path and media_playback service/owner may reference prior step results, e.g. owner:{"$ref":"observe.result.players.0.owner"}; expected states must remain literal, never copied from the observation being verified. Resolved identities are validated again before observation.'
    description += ' Control conditions may reference a complete target from desktop.controls.resolve, e.g. target:{"$ref":"resolve.result.target"}, or individual identity fields; control property and expected value/text remain literal.'
    description += " Settings conditions: power_profile + expected(exact native profile); screen_brightness + expected(integer percent 0-100, PowerDevil-managed display only, tolerance 1 percent); default_application + mime_type/expected(exact .desktop handler); night_light_state + property(enabled,running,inhibited)/expected boolean. All require fresh native readback and never modify settings. A MIME handler match is not proof the application can open the file."
    registry.register(
        ToolSpec(
            "agent.verify_conditions",
            "AGENT",
            description,
            Permission.SAFE,
            object_schema({"conditions": conditions}, ["conditions"]),
            verify,
            read_only=True,
            timeout_seconds=45,
        )
    )
    registry.register(
        ToolSpec(
            "agent.execute_plan",
            "AGENT",
            'Execute up to 32 ordered typed steps with nested {group,steps} subgoals and final observed conditions. Step: {id,tool,arguments}; id is a unique short alphanumeric string, e.g. create_file or "1"; tool is a dotted registered name such as files.text.create, not files__text__create. Arguments may use {"$ref":"earlier_id.result.field"}. Conditions are predicates, never tool calls, e.g. {"kind":"file_text","path":"/exact/file","expected":"requested contents"}. Repeated mutations need repeat_intentionally:true. Read-only agent.wait_for and agent.verify_conditions may separate actions; wait for an observed condition instead of repeating navigation or clicks. All other nested agent tools, power/admin and confirmation-gated actions are prohibited. The whole plan still has a 120-second maximum budget. On failure inspect evidence and change strategy, never blindly replay the whole plan. A verified condition does not prove all user intent was understood. '
            + description,
            Permission.LOW_RISK,
            object_schema(
                {
                    "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {"type": "object"},
                    },
                    "conditions": conditions,
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                ["goal", "steps", "conditions"],
            ),
            execute,
            timeout_seconds=125,
            verification="Fresh declared predicates after ordered substeps",
        )
    )
