"""
Per-language production profiles: one shared engine, one settings profile per language, so an
experiment on one language can't silently change the other. CLI flags override a profile.

History worth keeping in mind before changing a profile:
  - en: the approved full-title English (2026-09-23) used plain text, Natural stability, no
    direction, no pause extension, no overlap. Tags + overlap are being added deliberately.
  - hi: Kuber J (PVC) drifted on v3 -> Voice Design v3 voice; remix 3 chosen for pace.
  - pause extension caused clipped onsets when placed by alignment; now silence-snapped, but OFF
    by default — pauses should come from the text (v3 follows punctuation/line breaks).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    lang: str
    voice: str                 # env var name or raw voice id
    stability: str             # natural | robust | creative
    use_direction: bool        # LLM emotion/prosody direction pass (Stage 2.5)
    overlap: bool              # lead-in + look-ahead, trimmed at real silence
    pause_extension: bool      # post-production minimum pauses (off by default)
    anchor: str | None = None  # approved reference clip for voice-similarity QC
    # Defaults (2026-09-28): keep the optional text/audio transforms OFF; do QC ASSESSMENT
    # (report a listen list) but no on-the-spot regeneration/patching.
    normalize: bool = False    # numbers/dates/units -> spoken words (Gemini)
    pronunciation: bool = False # apply the pronunciation dictionary (alias respellings)
    qc_assess: bool = True     # run Scribe + forced-alignment checks -> report + listen list
    qc_fix: bool = False       # auto-regenerate chunks that fail / drift (requires qc_assess)


# The switchable checkpoints, in pipeline order. run_jobs.py can flip any of these per manifest
# or per job; a job's "steps" override the manifest's, which override the language profile.
STEP_KEYS = ("normalize", "pronunciation", "direction", "overlap",
             "qc_assess", "qc_fix", "pause_extension")


def resolve_steps(lang: str, *overrides: dict | None) -> dict:
    """Profile defaults, then each overrides dict applied in order (later wins). `direction`
    maps to the profile's `use_direction`. Unknown keys are ignored. `qc_fix` implies `qc_assess`.
    Back-compat: a legacy `qc` key sets both assess+fix."""
    prof = PROFILES[lang]
    steps = {"normalize": prof.normalize, "pronunciation": prof.pronunciation,
             "direction": prof.use_direction, "overlap": prof.overlap,
             "qc_assess": prof.qc_assess, "qc_fix": prof.qc_fix,
             "pause_extension": prof.pause_extension}
    for ov in overrides:
        for k, v in (ov or {}).items():
            if v is None:
                continue
            if k == "qc":  # legacy single switch -> both parts
                steps["qc_assess"] = steps["qc_fix"] = bool(v)
            elif k in steps:
                steps[k] = bool(v)
    if steps["qc_fix"]:
        steps["qc_assess"] = True  # fixing needs assessment to decide what to fix
    return steps


PROFILES = {
    # Voice + stability validated on the Key Idea 1 pilot (2026-09-24). Optional transforms
    # (normalize/pronunciation/direction/pause_extension) default OFF; turn them on per manifest/job.
    "en": LanguageProfile(lang="en", voice="ancient_wisdom_speaker_english_indian", stability="natural",
                          use_direction=False, overlap=True, pause_extension=False,
                          anchor="data/anchors/en_ancient_wisdom_speaker.wav"),
    # Natural chosen over Creative — Creative's register jumped at the 0:52 join; Natural was consistent.
    "hi": LanguageProfile(lang="hi", voice="ZXdqz9XCKNwPN16HnQWr", stability="natural",
                          use_direction=False, overlap=True, pause_extension=False,
                          anchor="data/anchors/hi_kitab_narrator_v2.wav"),
}
