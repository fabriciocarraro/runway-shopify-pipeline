"""
Motion style presets for Catalog → Campaign.

Each entry maps a ``--style`` name to the motion + lighting prompt that steers
the gen4.5 generation (the product title is appended at submit time).

Add your own preset by dropping a new ``"name": "prompt"`` entry below — the CLI
discovers it automatically: it becomes a valid ``--style`` choice with no other
code changes. Keep prompts to a sentence or two describing camera move, light,
and mood; the catalog photo stays the anchor frame, so don't restyle the product.
"""

STYLES = {
    "studio": (
        "Cinematic product showcase: slow push-in on the product, soft studio "
        "lighting, gentle rotation, premium commercial look, clean background"
    ),
    "lifestyle": (
        "Lifestyle product shot: natural light, shallow depth of field, subtle "
        "handheld camera drift, warm inviting mood, product stays in focus"
    ),
    "dramatic": (
        "Dramatic product reveal: dark background, sweeping rim light, slow "
        "orbit around the product, high-contrast premium commercial"
    ),
}
