"""
Pipeline entrypoint — glue only; behavior lives in content/, normalization/, chunking/,
generation/, tts/, pronunciation/.

    # 1. text prep: normalize + chunk -> work/<slug>/<lang>/chunks.json (review before spending credits)
    .venv/bin/python cli.py prepare ../00191410-....json --lang en hi

    # 2. generate audio for some or all sections (idempotent — unchanged chunks are skipped)
    .venv/bin/python cli.py generate work/SUM-737-your-brain-on-nature/en \\
        --voice ancient_wisdom_speaker_english_indian --section key_idea_1

    # ad-hoc normalization check
    .venv/bin/python cli.py normalize "Just 20 minutes a day" --lang en
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import load_settings


def cmd_prepare(args: argparse.Namespace) -> None:
    from content.kitab_loader import load_kitab_json
    from prepare import prepare_language, work_dir

    title = load_kitab_json(Path(args.content))
    for lang in args.lang:
        chunks = prepare_language(title, lang)
        sizes = [c.chars for c in chunks]
        print(f"[{lang}] {len(chunks)} chunks, {sum(sizes)} chars "
              f"(min {min(sizes)}, max {max(sizes)}) -> {work_dir(title, lang) / 'chunks.json'}")
        for c in chunks:
            print(f"    {c.id:16} {c.chars:4d}  -> {c.boundary_after}")


def cmd_generate(args: argparse.Namespace) -> None:
    from generation.generator import Generator
    from prepare import load_chunks
    from tts.client import ElevenLabsClient, VoicePreset

    settings = load_settings()
    work = Path(args.workdir)
    chunks = load_chunks(work / "chunks.json")
    if args.section:
        chunks = [c for c in chunks if c.section in args.section]
    if not chunks:
        sys.exit("no chunks match the selection")

    preset = VoicePreset(
        voice_id=settings.voice_ids.get(args.voice, args.voice),
        language_code=chunks[0].lang,
        stability=args.stability,
        seed=args.seed,
        use_pvc_as_ivc=args.use_pvc_as_ivc,
    )
    out_dir = work / args.run_name if args.run_name else work
    gen = Generator(ElevenLabsClient(settings.eleven_labs_api_key), preset, out_dir)
    print(f"generating {len(chunks)} chunk(s), {sum(c.chars for c in chunks)} chars -> {out_dir}")
    arts = gen.generate(chunks, force_ids=set(args.force or []), seed=args.retry_seed)
    print(f"done: {sum(not a.skipped for a in arts)} generated, {sum(a.skipped for a in arts)} skipped (unchanged)")


def _run_chunk_ids(run_dir: Path) -> set[str]:
    m = run_dir / "manifest.jsonl"
    return {json.loads(l)["chunk_id"] for l in m.read_text().splitlines() if l.strip()} if m.exists() else set()


def _run_chunks(run_dir: Path):
    """Chunks that were actually generated in this run dir, in title order."""
    from prepare import load_chunks
    work = run_dir if (run_dir / "chunks.json").exists() else run_dir.parent
    manifest = run_dir / "manifest.jsonl"
    done = {json.loads(l)["chunk_id"] for l in manifest.read_text().splitlines() if l.strip()} if manifest.exists() else set()
    return [c for c in load_chunks(work / "chunks.json") if c.id in done]


def cmd_qc(args: argparse.Namespace) -> None:
    from qc.gate import run_qc

    settings = load_settings()
    run_dir = Path(args.run_dir)
    chunks = _run_chunks(run_dir)
    results = run_qc(run_dir, chunks, chunks[0].lang, settings.eleven_labs_api_key,
                     Path(args.anchor) if args.anchor else None)
    for q in results:
        m = q.metrics
        print(f"{q.verdict.upper():5} {q.chunk_id:15} wer={m.get('wer')} cer={m.get('cer')} "
              f"sim={m.get('speaker_sim_anchor')} pitch={m.get('pitch_hz')} wps={m.get('words_per_sec')} "
              f"rms={m.get('speech_rms_db')}")
        for r in q.reasons:
            print(f"        {r}")
        for d in q.diffs[:6]:
            print(f"        diff {d['op']}: script={d['script']!r} heard={d['heard']!r}")
    print(f"\nreport: {run_dir / 'qc_report.json'}")


def cmd_assemble(args: argparse.Namespace) -> None:
    from assembly.joiner import assemble

    run_dir = Path(args.run_dir)
    chunks = _run_chunks(run_dir)
    qc_path = run_dir / "qc_report.json"
    qc_metrics = ({c["chunk_id"]: c["metrics"] for c in json.loads(qc_path.read_text())["chunks"]}
                  if qc_path.exists() else {})
    if not qc_metrics:
        print("note: no qc_report.json — pace matching disabled (run `qc` first)")
    r = assemble(run_dir, chunks, qc_metrics, out_name=args.name)
    print(f"target pace {r['target_words_per_sec']} syll/s | target speech level {r['target_speech_rms_db']} dB | room tone {r['room_tone_db']} dB")
    for e in r["chunks"]:
        print(f"  {e['chunk_id']:15} trim lead {e['trimmed_lead_sec']}s tail {e['trimmed_tail_sec']}s | "
              f"pauses capped {e['pauses_shortened']} | tempo x{e['tempo']} | gain {e['gain_db']:+} dB | {e['duration_sec']}s"
              + (f"  <-- pace {e['pace_deviation']:+.0%} off title median: REGENERATE suggested" if e["regenerate_suggested"] else ""))
    f = r["final"]
    print(f"final: {f['duration_sec']}s | RMS {f['rms_db']} dB | peak {f['peak_db']} dB | noise floor {f['noise_floor_db']} dB | "
          f"ACX-style spec {'PASS' if f['acx_ok'] else 'FAIL'}")
    print(f"-> {r['outputs']['mastered_mp3']}")


def cmd_produce(args: argparse.Namespace) -> None:
    from prepare import load_chunks
    from production import produce
    from tts.client import VoicePreset

    from profiles import PROFILES

    settings = load_settings()
    work = Path(args.workdir)
    all_chunks = load_chunks(work / "chunks.json")
    prof = PROFILES.get(args.profile or all_chunks[0].lang)
    args.voice = args.voice or prof.voice
    args.stability = args.stability or prof.stability
    if args.direction is None and prof.use_direction and (work / "direction.json").exists():
        args.direction = str(work / "direction.json")
    overlap = prof.overlap if args.overlap is None else args.overlap == "on"
    args.anchor = args.anchor or prof.anchor
    pause_ext = prof.pause_extension if args.pause_extension is None else args.pause_extension == "on"
    print(f"profile {prof.lang}: voice={args.voice} stability={args.stability} direction={bool(args.direction)} "
          f"overlap={overlap} pause_extension={pause_ext}")
    intended_pace = None
    if args.direction:
        from direction.director import apply_direction
        all_chunks, intended_pace = apply_direction(all_chunks, Path(args.direction))
    chunks = [c for c in all_chunks if not args.section or c.section in args.section]
    preset = VoicePreset(voice_id=settings.voice_ids.get(args.voice, args.voice),
                         language_code=chunks[0].lang, stability=args.stability, seed=args.seed)
    run_dir = work / args.run_name
    force = getattr(args, "force_ids", None)
    if force:
        chunks = [c for c in all_chunks if c.id in _run_chunk_ids(run_dir)] or chunks
    qc_assess = prof.qc_assess if getattr(args, "qc_assess", None) is None else args.qc_assess
    qc_fix = prof.qc_fix if getattr(args, "qc_fix", None) is None else args.qc_fix
    s = produce(run_dir, chunks, preset, settings.eleven_labs_api_key, args.max_retries,
                Path(args.anchor) if args.anchor else None, intended_pace, overlap=overlap,
                all_chunks=all_chunks, pause_extension=pause_ext,
                force_ids=force, force_seed=getattr(args, "force_seed", None),
                qc_assess=qc_assess, qc_fix=qc_fix)
    f = s["final"]
    print(f"\nfinal {f['duration_sec']}s | RMS {f['rms_db']} | peak {f['peak_db']} | floor {f['noise_floor_db']} | "
          f"spec {'PASS' if f['acx_ok'] else 'FAIL'}")
    print(f"still failing: {s['still_failing'] or 'none'} | retries: {len(s['retries'])}")
    print(f"listen list ({len(s['listen_list'])}):")
    for item in s["listen_list"]:
        print(f"  {item['chunk']}: {'; '.join(item['reasons'])}")


def _parse_ts(v: str) -> float:
    parts = [float(x) for x in v.split(":")]
    return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0]


def cmd_fix(args: argparse.Namespace) -> None:
    """Regenerate ONLY the chunks at the given timestamps / ids, re-check, re-assemble."""
    import random
    from production import chunks_at_times
    run_dir = Path(args.run_dir)
    ids = set(args.chunk or [])
    if args.at:
        mapping = chunks_at_times(run_dir, [_parse_ts(t) for t in args.at])
        for t in args.at:
            cid = mapping.get(_parse_ts(t))
            print(f"  {t} -> {cid or 'no chunk found'}")
            if cid:
                ids.add(cid)
    if not ids:
        sys.exit("nothing to fix")
    args.workdir = str(run_dir.parent)
    args.run_name = run_dir.name
    args.force_ids = ids
    args.force_seed = random.randint(1, 2**31)
    args.section = None
    args.qc_assess, args.qc_fix = True, True  # a manual fix regenerates and re-checks the chunk
    print(f"regenerating {sorted(ids)} with seed {args.force_seed}; previous takes kept in chunks/versions/")
    cmd_produce(args)


def cmd_revert(args: argparse.Namespace) -> None:
    """Put a previous take of a chunk back (then re-assemble)."""
    import shutil
    run_dir = Path(args.run_dir)
    vdir = run_dir / "chunks" / "versions"
    takes = sorted(vdir.glob(f"{args.chunk}.*.wav"))
    if not takes:
        sys.exit(f"no previous takes for {args.chunk}")
    if args.take is None:
        for i, t in enumerate(takes):
            print(f"  {i}: {t.name}")
        print("re-run with --take N to restore one")
        return
    src = takes[args.take]
    cur = run_dir / "chunks" / f"{args.chunk}.wav"
    shutil.copy(cur, vdir / f"{args.chunk}.reverted-from.wav")
    shutil.copy(src, cur)
    al = src.with_name(src.name.replace(".wav", ".alignment.json"))
    if al.exists():
        shutil.copy(al, run_dir / "chunks" / f"{args.chunk}.alignment.json")
    print(f"restored {src.name}; now run: cli.py qc {run_dir} && cli.py assemble {run_dir}")


def cmd_direct(args: argparse.Namespace) -> None:
    from direction.director import build_direction
    from prepare import load_chunks

    settings = load_settings()
    work = Path(args.workdir)
    meta = json.loads((work / "chunks.json").read_text())
    chunks = load_chunks(work / "chunks.json")
    doc = build_direction(work, chunks, meta["title"], meta["author"], settings.gemini_api_key)
    p = doc["profile"]
    print(f"persona: {p['narrator_persona']}\nregister: {p['base_register']}\nallowed tags: {p['allowed_tags']}")
    ok = sum(c["accepted"] for c in doc["chunks"])
    print(f"\n{ok}/{len(doc['chunks'])} chunks accepted (rest fall back to plain text)")
    for c in doc["chunks"]:
        mark = "ok  " if c["accepted"] else "FALL"
        print(f"  {mark} {c['chunk_id']:15} pace={c['intended_pace']:7} [{c['lead_tag']}]"
              + (f"  problems={c['problems']}" if c["problems"] else ""))
    print(f"\npronunciation candidates: {[x['term'] for x in doc['pronunciation_candidates']]}")
    print(f"sfx suggestions: {[(x['chunk_id'], x['sound']) for x in doc['sfx_suggestions']]}")
    print(f"-> {work / 'direction.json'}")


def cmd_normalize(args: argparse.Namespace) -> None:
    from normalization.cache_store import InMemoryCache, JSONFileCache
    from normalization.llm_normalizer import GeminiNormalizer, NullNormalizer
    from normalization.pipeline import NormalizationPipeline

    settings = load_settings()
    cache = InMemoryCache() if args.dry_run else JSONFileCache(settings.normalization_cache_path)
    llm = NullNormalizer() if args.dry_run else GeminiNormalizer(settings.gemini_api_key, settings.gemini_model)
    result = NormalizationPipeline(cache, llm, language_hint=args.lang).normalize(args.text)
    print(json.dumps({"normalized": result.normalized_text, "term_map": result.term_map,
                      "new_terms_llm": result.new_terms, "cached": result.cached_terms},
                     indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="normalize + chunk a Kitab content JSON")
    p.add_argument("content")
    p.add_argument("--lang", nargs="+", default=["en", "hi"])
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("generate", help="generate audio for prepared chunks")
    p.add_argument("workdir", help="work/<slug>/<lang>")
    p.add_argument("--voice", required=True, help="env var name or raw voice id")
    p.add_argument("--section", nargs="*", help="e.g. key_idea_1 intro (default: all)")
    p.add_argument("--stability", choices=["natural", "robust"], default="natural")
    p.add_argument("--seed", type=int, default=20260923)
    p.add_argument("--use-pvc-as-ivc", action="store_true")
    p.add_argument("--run-name", help="subfolder for an experiment variant (keeps runs separate)")
    p.add_argument("--force", nargs="*", help="chunk ids to regenerate even if unchanged")
    p.add_argument("--retry-seed", type=int, help="seed override for regeneration")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("qc", help="run the QC gate on a generated run")
    p.add_argument("run_dir", help="work/<slug>/<lang>[/<run-name>]")
    p.add_argument("--anchor", help="approved reference wav (default: first chunk of the run)")
    p.set_defaults(func=cmd_qc)

    p = sub.add_parser("assemble", help="trim, pace/level match, join, master a run")
    p.add_argument("run_dir")
    p.add_argument("--name", default="narration")
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("produce", help="generate -> QC -> auto-regenerate -> assemble, end to end")
    p.add_argument("workdir", help="work/<slug>/<lang>")
    p.add_argument("--profile", choices=["en", "hi"], help="language profile (default: the chunks' language)")
    p.add_argument("--voice", help="override the profile's voice")
    p.add_argument("--section", nargs="*")
    p.add_argument("--stability", choices=["natural", "robust", "creative"], help="override the profile")
    p.add_argument("--overlap", choices=["on", "off"], help="override the profile")
    p.add_argument("--pause-extension", choices=["on", "off"], help="override the profile")
    p.add_argument("--seed", type=int, default=20260923)
    p.add_argument("--max-retries", type=int, default=2)
    p.add_argument("--anchor", help="approved reference wav for voice similarity")
    p.add_argument("--run-name", default="full")
    p.add_argument("--direction", default=None, help="direction.json (default: work dir's direction.json if the profile uses direction)")
    p.set_defaults(func=cmd_produce)

    p = sub.add_parser("fix", help="regenerate only the chunks at given timestamps/ids, then re-check + re-assemble")
    p.add_argument("run_dir", help="work/<slug>/<lang>/<run-name>")
    p.add_argument("--at", nargs="*", help="timestamps in the final file, e.g. 2:15 7:40")
    p.add_argument("--chunk", nargs="*", help="chunk ids, e.g. key_idea_3_02")
    p.add_argument("--profile", choices=["en", "hi"])
    p.add_argument("--voice"); p.add_argument("--stability"); p.add_argument("--overlap"); p.add_argument("--pause-extension")
    p.add_argument("--seed", type=int, default=20260923); p.add_argument("--max-retries", type=int, default=2)
    p.add_argument("--anchor"); p.add_argument("--direction", default=None)
    p.set_defaults(func=cmd_fix)

    p = sub.add_parser("revert", help="list / restore previous takes of a chunk")
    p.add_argument("run_dir"); p.add_argument("chunk"); p.add_argument("--take", type=int)
    p.set_defaults(func=cmd_revert)

    p = sub.add_parser("direct", help="LLM performance-direction pass (structured output) for a prepared title")
    p.add_argument("workdir", help="work/<slug>/<lang>")
    p.set_defaults(func=cmd_direct)

    p = sub.add_parser("normalize", help="ad-hoc normalization of one string")
    p.add_argument("text")
    p.add_argument("--lang", default="en")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_normalize)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
