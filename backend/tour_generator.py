"""
Tour Generator — AI-powered spatial tour schema generation.

Takes raw property data (address, rooms, sqft, features, listing description)
and produces a complete tour navigation schema: room waypoints with camera poses,
hotspot anchors, floor plan geometry, AI narration, and stat chips.

The generated schema is directly consumable by the frontend TourExperience
component — it matches the Waypoint[] + CameraPose contract in tourData.ts.

Pipeline:
  1. Receive property metadata (structured or free-text listing)
  2. LLM generates spatial layout (room arrangement, dimensions, camera poses)
  3. Validate JSON against the tour schema
  4. Optionally combine with spatial_agent's .splat for hybrid rendering

Uses Bedrock (Llama-70B) for generation with guided JSON output.
Falls back to Claude-3 Sonnet if Llama fails.
"""

import json
import logging
import os
from typing import Optional

from ml_forge.bedrock_client import invoke_bedrock_model, PRIMARY_MODEL, SECONDARY_MODEL

logger = logging.getLogger("oracle.tour_generator")

TOUR_SCHEMA = {
    "type": "object",
    "required": ["property", "waypoints", "footprint", "tour_order"],
    "properties": {
        "property": {
            "type": "object",
            "required": ["name", "tagline", "style"],
            "properties": {
                "name": {"type": "string"},
                "tagline": {"type": "string"},
                "style": {"type": "string", "enum": [
                    "luxury-penthouse", "modern-home", "classic-estate",
                    "urban-loft", "coastal-villa", "mountain-retreat"
                ]},
            },
        },
        "footprint": {
            "type": "object",
            "required": ["width", "depth"],
            "properties": {
                "width": {"type": "number"},
                "depth": {"type": "number"},
            },
        },
        "waypoints": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "name", "tag", "pose", "hotspot", "narration", "plan", "stats"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "tag": {"type": "string"},
                    "pose": {
                        "type": "object",
                        "required": ["position", "target"],
                        "properties": {
                            "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                            "target": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                        },
                    },
                    "hotspot": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                    "narration": {"type": "string"},
                    "plan": {
                        "type": "object",
                        "required": ["center", "size"],
                        "properties": {
                            "center": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                            "size": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                        },
                    },
                    "stats": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["label", "value"],
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "tour_order": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM_PROMPT = """You are Oracle's Spatial Architect AI. You generate 3D virtual tour navigation schemas for luxury real estate properties.

Given property data, you produce a complete spatial tour with:
- Room waypoints (4-8 rooms) with realistic camera poses for a first-person walkthrough
- Floor hotspot positions for clickable navigation pucks
- 2D floor plan geometry (center + size in metres) for the minimap
- Luxury narration for each room (vivid, architectural, evocative)
- Property stats per room (materials, dimensions, features)

COORDINATE SYSTEM:
- x = right (east), z = toward camera (south), y = up
- Floor slab at y = 0, eye height at y = 1.65
- Camera positions: eye-height (1.65) for FPV, elevated for overview
- Hotspots: always at y = 0.04 (just above floor)
- Room centers on the 2D plan use [x, z] coordinates
- Footprint width spans the x-axis, depth spans the z-axis
- Keep all rooms within the footprint bounds (±width/2, ±depth/2)

SPATIAL RULES:
- Adjacent rooms share walls (no floating disconnected rooms)
- Camera target should point toward the room's focal feature
- Narration should be 1-2 sentences, luxury real estate voice
- Stats should highlight materials, finishes, dimensions, and features
- Tour order should follow a natural walkthrough path (entry → social → private)
- Room IDs must be lowercase kebab-case
- Footprint should be realistic for the property size (1 sqft ≈ 0.093 m²)

Respond with ONLY valid JSON matching the schema. No markdown fences, no prose."""

USER_PROMPT_TEMPLATE = """Generate a complete 3D virtual tour schema for this property:

{property_data}

Requirements:
- Generate {room_count} rooms based on the property features
- Total footprint should reflect the square footage (~{sqft_m2:.0f} m² = {sqft} sqft)
- Style: {style}
- Include luxury narration appropriate to the property's market positioning
- Camera poses should create an immersive first-person walkthrough experience

Return the tour schema as strict JSON."""


def _estimate_style(data: dict) -> str:
    price = data.get("price", 0)
    sqft = data.get("sqft", 0)
    features = " ".join(data.get("features", [])).lower()
    description = data.get("description", "").lower()

    if any(w in features or w in description for w in ("penthouse", "skyline", "high-rise", "elevator")):
        return "luxury-penthouse"
    if any(w in features or w in description for w in ("ocean", "beach", "coastal", "waterfront")):
        return "coastal-villa"
    if any(w in features or w in description for w in ("mountain", "cabin", "lodge", "timber")):
        return "mountain-retreat"
    if any(w in features or w in description for w in ("loft", "warehouse", "industrial", "urban")):
        return "urban-loft"
    if price > 2_000_000 or sqft > 5000:
        return "classic-estate"
    return "modern-home"


def _estimate_room_count(data: dict) -> int:
    beds = data.get("bedrooms", 0)
    baths = data.get("bathrooms", 0)
    sqft = data.get("sqft", 0)
    rooms_from_sqft = max(4, min(8, sqft // 400))
    rooms_from_beds = beds + 2  # beds + kitchen + living
    return max(4, min(8, max(rooms_from_sqft, rooms_from_beds)))


def _validate_tour_data(data: dict) -> tuple[bool, str]:
    """Validate the generated tour data against spatial constraints."""
    if not isinstance(data, dict):
        return False, "Response is not a JSON object"
    for field in ("property", "waypoints", "footprint", "tour_order"):
        if field not in data:
            return False, f"Missing required field: {field}"
    waypoints = data["waypoints"]
    if not isinstance(waypoints, list) or len(waypoints) < 3:
        return False, f"Need at least 3 waypoints, got {len(waypoints) if isinstance(waypoints, list) else 0}"
    for wp in waypoints:
        if not isinstance(wp.get("pose", {}).get("position"), list) or len(wp["pose"]["position"]) != 3:
            return False, f"Waypoint '{wp.get('id', '?')}' has invalid pose.position"
        if not isinstance(wp.get("hotspot"), list) or len(wp["hotspot"]) != 3:
            return False, f"Waypoint '{wp.get('id', '?')}' has invalid hotspot"
    tour_ids = set(data["tour_order"])
    wp_ids = {wp["id"] for wp in waypoints}
    missing = tour_ids - wp_ids
    if missing:
        return False, f"tour_order references unknown waypoint IDs: {missing}"
    return True, "ok"


async def generate_tour(property_data: dict) -> Optional[dict]:
    """Generate a complete spatial tour schema from property metadata.

    Args:
        property_data: Dict with keys like address, sqft, bedrooms, bathrooms,
                      features (list), description (string), price, etc.

    Returns:
        Complete tour schema dict matching the frontend's expected format,
        or None if generation fails.
    """
    sqft = property_data.get("sqft", 2000)
    sqft_m2 = sqft * 0.093
    style = _estimate_style(property_data)
    room_count = _estimate_room_count(property_data)

    user_prompt = USER_PROMPT_TEMPLATE.format(
        property_data=json.dumps(property_data, indent=2, default=str),
        room_count=room_count,
        sqft_m2=sqft_m2,
        sqft=sqft,
        style=style,
    )

    # Try Llama-70B first (faster, cheaper)
    raw = invoke_bedrock_model(PRIMARY_MODEL, f"{SYSTEM_PROMPT}\n\n{user_prompt}", max_tokens=4096)

    if not raw:
        logger.warning("Primary model failed, falling back to secondary")
        raw = invoke_bedrock_model(SECONDARY_MODEL, f"{SYSTEM_PROMPT}\n\n{user_prompt}", max_tokens=4096)

    if not raw:
        logger.error("Both models failed to generate tour data")
        return None

    # Parse JSON from response
    parsed = _extract_json(raw)
    if not parsed:
        logger.error("Failed to parse JSON from model response: %s", raw[:200])
        return None

    valid, msg = _validate_tour_data(parsed)
    if not valid:
        logger.error("Generated tour data failed validation: %s", msg)
        return None

    # Inject computed fields the frontend expects
    parsed["explore_pose"] = {
        "position": [parsed["footprint"]["width"] * 0.5, 6, parsed["footprint"]["depth"] * 0.7],
        "target": [0, 1.2, 0],
    }
    parsed["dollhouse_pose"] = {
        "position": [parsed["footprint"]["width"] * 0.6, 13, parsed["footprint"]["depth"] * 0.8],
        "target": [0, 0.5, 0],
    }

    logger.info("Generated tour: %d waypoints for '%s' (%s)",
                len(parsed["waypoints"]), property_data.get("address", "unknown"), style)
    return parsed


def _extract_json(text: str) -> Optional[dict]:
    """Extract JSON object from model response, handling markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
