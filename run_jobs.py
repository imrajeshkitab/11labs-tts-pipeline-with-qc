"""
Batch runner: manifest JSON -> pull content from RMS -> generate per language with the manifest's
voice -> save named outputs in output/<content_type>/<lang>/.

Manifest (see jobs.example.json):
    {
      "output_root": "output",
      "jobs": [
        {"content_id": "SUM-737", "content_type": "summary", "languages": ["en","hi"],
         "voices": {"en": {"name": "...", "voice_id": "..."},
                    "hi": {"name": "...", "voice_id": "ZXdqz9XCKNwPN16HnQWr"}}}
      ]
    }
- content_id matches the DB source_id or the uuid; content_type is summary|bite|journey.
- voice_id may be an env-var name (resolved via .env) or a raw ElevenLabs id. If a language has no
  voice in the manifest, the language profile's default voice is used.
- Output file: <content_id>_<ddmmyyHHMMSS>_<title-slug>.mp3

    .venv/bin/python run_jobs.py jobs.json
    .venv/bin/python run_jobs.py jobs.json --dry-run     # fetch + prepare + direct, no audio
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

from config import PACKAGE_ROOT, load_settings
from content.rms_fetch import fetch_title
from prepare import prepare_language, work_dir
from profiles import PROFILES, resolve_steps


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "untitled"


def output_path(root: Path, content_type: str, lang: str, content_id: str, title: str, when: str) -> Path:
    fname = f"{content_id}_{when}_{_slug(title)}.mp3"
    return root / content_type / lang / fname


def run_job(job: dict, settings, output_root: Path, dry_run: bool, manifest_steps: dict | None) -> list[dict]:
    from direction.director import apply_direction, build_direction
    from production import produce
    from tts.client import VoicePreset

    cid, ctype = job["content_id"], job["content_type"]
    langs = job.get("languages") or ["en", "hi"]
    voices = job.get("voices") or {}
    results = []

    title = fetch_title(cid, ctype)
    print(f"\n=== {ctype} {cid}: {title.title!r} ({', '.join(title.sections)})")

    for lang in langs:
        if lang not in title.sections:
            print(f"  [{lang}] not present in this content — skipped")
            results.append({"content_id": cid, "lang": lang, "status": "missing_language"})
            continue

        prof = PROFILES.get(lang)
        # checkpoint toggles: profile defaults <- manifest "steps" <- job "steps"
        steps = resolve_steps(lang, manifest_steps, job.get("steps"))
        vcfg = voices.get(lang) or {}
        raw_voice = vcfg.get("voice_id") or (prof.voice if prof else None)
        if not raw_voice:
            results.append({"content_id": cid, "lang": lang, "status": "no_voice"})
            continue
        voice_id = settings.voice_ids.get(raw_voice, raw_voice)

        chunks = prepare_language(title, lang, normalize=steps["normalize"],
                                 pronunciation=steps["pronunciation"])
        wdir = work_dir(title, lang)
        intended_pace = None
        if steps["direction"]:
            build_direction(wdir, chunks, title.title, title.author, settings.gemini_api_key)
            chunks, intended_pace = apply_direction(chunks, wdir / "direction.json")

        when = time.strftime("%d%m%y%H%M%S")
        out = output_path(output_root, ctype, lang, cid, title.title, when)
        on = [k for k in ("normalize", "pronunciation", "direction", "overlap",
                          "qc_assess", "qc_fix", "pause_extension") if steps[k]]
        print(f"  [{lang}] voice {vcfg.get('name') or raw_voice} | steps: {', '.join(on) or 'none'} -> {out}")
        if dry_run:
            results.append({"content_id": cid, "lang": lang, "status": "dry_run",
                            "chunks": len(chunks), "steps": steps, "planned_output": str(out)})
            continue

        preset = VoicePreset(voice_id=voice_id, language_code=lang,
                             stability=(prof.stability if prof else "natural"))
        run_dir = wdir / "batch"
        summary = produce(run_dir, chunks, preset, settings.eleven_labs_api_key,
                          anchor_wav=(Path(prof.anchor) if prof and prof.anchor else None),
                          intended_pace=intended_pace, overlap=steps["overlap"],
                          all_chunks=chunks, pause_extension=steps["pause_extension"],
                          qc_assess=steps["qc_assess"], qc_fix=steps["qc_fix"])
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(summary["output"], out)
        results.append({"content_id": cid, "lang": lang, "status": "ok", "output": str(out),
                        "steps": steps, "duration_sec": summary["final"]["duration_sec"],
                        "still_failing": summary["still_failing"], "run_dir": str(run_dir)})
        print(f"  [{lang}] done: {out}  ({summary['final']['duration_sec']}s, "
              f"failing: {summary['still_failing'] or 'none'})")
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest")
    ap.add_argument("--dry-run", action="store_true", help="fetch + prepare + direct only; no audio")
    args = ap.parse_args()

    settings = load_settings()
    manifest = json.loads(Path(args.manifest).read_text())
    output_root = (PACKAGE_ROOT / manifest.get("output_root", "output"))
    manifest_steps = manifest.get("steps")  # applies to every job unless the job overrides it
    all_results = []
    for job in manifest["jobs"]:
        try:
            all_results += run_job(job, settings, output_root, args.dry_run, manifest_steps)
        except Exception as exc:  # noqa: BLE001 — one bad job shouldn't stop the batch
            print(f"  ERROR on {job.get('content_id')}: {type(exc).__name__}: {exc}")
            all_results.append({"content_id": job.get("content_id"), "status": "error", "error": str(exc)})

    log = output_root / f"run_{time.strftime('%d%m%y%H%M%S')}.json"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    ok = sum(r["status"] == "ok" for r in all_results)
    print(f"\n{ok}/{len(all_results)} outputs produced. Log: {log}")
    sys.exit(0 if ok or args.dry_run else 1)


if __name__ == "__main__":
    main()
