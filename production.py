"""
End-to-end production loop for one title + language:

    generate (idempotent) -> QC -> regenerate failures & pace outliers with a new seed (max N)
    -> re-QC -> assemble + master -> summary (what still needs a human listen)

Glue over generation/, qc/, assembly/ — no audio or QC logic lives here.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from assembly.joiner import assemble
from generation.generator import Generator
from qc.gate import run_qc
from tts.client import ElevenLabsClient, VoicePreset

PACE_REGEN_THRESHOLD = 0.15   # syllables/sec deviation beyond which one regeneration is tried
PACE_STABLE_TOLERANCE = 0.05  # retry landed within this of the previous pace -> text-driven, stop


def _paces(results) -> dict[str, float]:
    return {q.chunk_id: q.metrics["syllables_per_sec"] for q in results if q.metrics.get("syllables_per_sec")}


def _pace_outliers(results) -> set[str]:
    rates = _paces(results)
    if len(rates) < 4:  # too few chunks for a stable median
        return set()
    med = float(np.median(list(rates.values())))
    return {cid for cid, r in rates.items() if abs(med / r - 1) > PACE_REGEN_THRESHOLD}


def build_lead_ins(chunks: list) -> dict[str, str]:
    """Overlap-and-trim lead-ins: previous chunk's last sentence, only within a section."""
    from generation.overlap import lead_in_for
    out = {}
    for prev, cur in zip(chunks, chunks[1:]):
        if prev.section == cur.section:
            lead = lead_in_for(prev.text)
            if lead:
                out[cur.id] = lead
    return out


def build_look_aheads(chunks: list, all_chunks: list) -> dict[str, str]:
    """Next chunk's first sentence (any section) so every chunk's last word can decay naturally."""
    from generation.overlap import look_ahead_for
    order = [c.id for c in all_chunks]
    by_id = {c.id: c for c in all_chunks}
    out = {}
    for c in chunks:
        i = order.index(c.id) if c.id in order else -1
        nxt = by_id[order[i + 1]].text if 0 <= i < len(order) - 1 else None
        out[c.id] = look_ahead_for(nxt, c.lang)
    return out


def produce(run_dir: Path, chunks: list, preset: VoicePreset, api_key: str,
            max_retries: int = 2, anchor_wav: Path | None = None,
            intended_pace: dict[str, str] | None = None, overlap: bool = True,
            all_chunks: list | None = None, pause_extension: bool = False,
            force_ids: set[str] | None = None, force_seed: int | None = None,
            qc_assess: bool = True, qc_fix: bool = False) -> dict:
    gen = Generator(ElevenLabsClient(api_key), preset, run_dir)
    lang = chunks[0].lang
    lead_ins = build_lead_ins(chunks) if overlap else {}
    look_aheads = build_look_aheads(chunks, all_chunks or chunks) if overlap else {}

    print(f"[1/4] generating {len(chunks)} chunks ({len(lead_ins)} with lead-in, {len(look_aheads)} with look-ahead)")
    gen.generate(chunks, lead_ins=lead_ins, look_aheads=look_aheads,
                 force_ids=force_ids, seed=force_seed if force_ids else None)

    # QC has two independent parts: ASSESS (run the checks -> report + listen list) and FIX
    # (auto-regenerate chunks that fail / drift). Fix requires assess.
    history, results = [], []
    if not qc_assess:
        print("[2/4] QC skipped (assessment off) — no checks, no report, no fixes")
    elif not qc_fix:
        print("[2/4] QC assessment only (fix off) — report the listen list, keep every take as-is")
        results = run_qc(run_dir, chunks, lang, api_key, anchor_wav)
    else:
        print("[2/4] QC assessment + fix — regenerate failing / drifting chunks")
        results = run_qc(run_dir, chunks, lang, api_key, anchor_wav)
        pace_tried: dict[str, float] = {}  # chunk -> pace before its (single) pace retry
        for attempt in range(1, max_retries + 1):
            failing = {q.chunk_id for q in results if q.verdict == "fail"}
            current = _paces(results)
            pace = set()
            for cid in _pace_outliers(results):
                if cid in pace_tried:
                    continue  # one pace retry only; if still off, pace is text-driven -> joiner stretches it
                pace.add(cid)
                pace_tried[cid] = current[cid]
            todo = failing | pace
            if not todo:
                break
            print(f"      retry {attempt}/{max_retries}: {sorted(todo)} "
                  f"(fail={sorted(failing)}, pace={sorted(pace)})")
            history.append({"attempt": attempt, "fail": sorted(failing), "pace": sorted(pace)})
            gen.generate([c for c in chunks if c.id in todo], force_ids=todo, seed=preset.seed + 1000 * attempt,
                         lead_ins=lead_ins, look_aheads=look_aheads)
            results = run_qc(run_dir, chunks, lang, api_key, anchor_wav)

    print("[3/4] assembling + mastering")
    metrics = {q.chunk_id: q.metrics for q in results}  # empty when qc off -> pace matching disabled
    report = assemble(run_dir, chunks, metrics, intended_pace=intended_pace, pause_extension=pause_extension)

    summary = {
        "qc_assess": qc_assess, "qc_fix": qc_fix,
        "retries": history,
        "still_failing": [q.chunk_id for q in results if q.verdict == "fail"],
        "listen_list": [{"chunk": q.chunk_id, "reasons": q.reasons} for q in results if q.verdict != "pass"],
        "pace_regen_suggested_after_retries": [e["chunk_id"] for e in report["chunks"] if e["regenerate_suggested"]],
        "final": report["final"],
        "output": report["outputs"]["mastered_mp3"],
    }
    (run_dir / "production_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[4/4] done -> {summary['output']}")
    return summary


def chunks_at_times(run_dir: Path, times_sec: list[float]) -> dict[float, str]:
    """Map timestamps in the assembled file back to chunk ids (from the joiner's own report)."""
    from assembly.joiner import PAUSES_SEC
    rep = json.loads((run_dir / "narration_assembly_report.json").read_text())
    boundary = {}
    for l in (run_dir / "manifest.jsonl").read_text().splitlines():
        if l.strip():
            e = json.loads(l); boundary[e["chunk_id"]] = e.get("boundary_after", "paragraph")
    spans, t = [], PAUSES_SEC["head"]
    for e in rep["chunks"]:
        end = t + e["duration_sec"]
        gap = PAUSES_SEC.get(boundary.get(e["chunk_id"]), PAUSES_SEC["paragraph"])
        spans.append((e["chunk_id"], t, end + gap))  # a pause belongs to the chunk before it
        t = end + gap
    out = {}
    for ts in times_sec:
        for cid, a, b in spans:
            if a - 0.5 <= ts < b:
                out[ts] = cid
                break
    return out
