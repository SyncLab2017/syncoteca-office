import json
from pathlib import Path
from crewai.tools import BaseTool


MOOD_TAGS = [
    "happy", "sad", "energetic", "calm", "tense", "romantic",
    "mysterious", "epic", "playful", "melancholic", "uplifting", "dark",
]

GENRE_TAGS = [
    "electronic", "ambient", "classical", "jazz", "pop", "rock", "hip-hop",
    "folk", "world", "cinematic", "orchestral", "acoustic",
]

INSTRUMENTATION_TAGS = [
    "piano", "guitar", "strings", "brass", "drums", "bass", "synth",
    "vocals", "choir", "woodwinds", "percussion", "a-cappella",
]


class MetadataTool(BaseTool):
    """Validate, normalize, and enrich music track metadata to DDEX/ISRC standards."""

    name: str = "metadata_processor"
    description: str = (
        "Validate and normalize track metadata. "
        "Input: track_data (dict) with fields like title, artist, bpm, key, mood, genre, etc. "
        "Returns normalized metadata JSON ready for database insertion."
    )

    def _run(self, track_data: dict) -> str:
        errors = []
        warnings = []
        normalized = {}

        # Required fields
        for field in ("title", "artist"):
            val = track_data.get(field, "").strip()
            if not val:
                errors.append(f"Missing required field: {field}")
            else:
                normalized[field] = val

        # ISRC validation (format: CC-XXX-YY-NNNNN)
        isrc = track_data.get("isrc", "").strip().upper().replace("-", "")
        if isrc:
            if len(isrc) == 12 and isrc[:2].isalpha():
                normalized["isrc"] = f"{isrc[:2]}-{isrc[2:5]}-{isrc[5:7]}-{isrc[7:]}"
            else:
                warnings.append(f"ISRC format invalid: {isrc}. Expected: CC-XXX-YY-NNNNN")

        # BPM
        bpm = track_data.get("bpm")
        if bpm:
            try:
                bpm_int = int(float(str(bpm)))
                if 40 <= bpm_int <= 300:
                    normalized["bpm"] = bpm_int
                else:
                    warnings.append(f"BPM out of range: {bpm_int}")
            except ValueError:
                warnings.append(f"Invalid BPM: {bpm}")

        # Key (musical key)
        key = track_data.get("key", "").strip()
        if key:
            normalized["key"] = key

        # Duration (seconds)
        duration = track_data.get("duration_seconds") or track_data.get("duration")
        if duration:
            try:
                normalized["duration_seconds"] = int(float(str(duration)))
            except ValueError:
                warnings.append(f"Invalid duration: {duration}")

        # Mood tags
        moods = track_data.get("mood", track_data.get("moods", []))
        if isinstance(moods, str):
            moods = [m.strip() for m in moods.split(",")]
        valid_moods = [m.lower() for m in moods if m.lower() in MOOD_TAGS]
        unknown_moods = [m for m in moods if m.lower() not in MOOD_TAGS]
        if valid_moods:
            normalized["mood"] = valid_moods
        if unknown_moods:
            warnings.append(f"Unknown mood tags (kept): {unknown_moods}")
            normalized["mood"] = normalized.get("mood", []) + unknown_moods

        # Genre
        genre = track_data.get("genre", "").lower().strip()
        if genre:
            normalized["genre"] = genre
            if genre not in GENRE_TAGS:
                warnings.append(f"Non-standard genre: {genre}")

        # Instrumentation
        instruments = track_data.get("instrumentation", track_data.get("instruments", []))
        if isinstance(instruments, str):
            instruments = [i.strip() for i in instruments.split(",")]
        normalized["instrumentation"] = [i.lower() for i in instruments]

        # Versions available
        for version in ("has_vocal", "has_instrumental", "has_stems"):
            val = track_data.get(version, False)
            normalized[version] = bool(val)

        # Rights holder info
        for field in ("composer", "publisher", "label", "territory"):
            val = track_data.get(field, "").strip()
            if val:
                normalized[field] = val

        # Pass-through optional fields
        for field in ("album", "year", "iswc", "language", "explicit"):
            val = track_data.get(field)
            if val is not None:
                normalized[field] = val

        report = []
        if errors:
            report.append("ERRORS (fix before upload):\n" + "\n".join(f"  ✗ {e}" for e in errors))
        if warnings:
            report.append("WARNINGS:\n" + "\n".join(f"  ⚠ {w}" for w in warnings))
        if not errors:
            report.append("STATUS: Ready for database upload")

        result = {
            "normalized_metadata": normalized,
            "validation": {
                "errors": errors,
                "warnings": warnings,
                "ready": len(errors) == 0,
            },
        }

        return "\n".join(report) + "\n\n" + json.dumps(result, ensure_ascii=False, indent=2)
