"""
Chunk list + frozen preset -> per-chunk WAV + alignment + manifest.

Idempotent by content hash (chunk text + preset fingerprint): re-running only generates chunks
that are new or changed — editing one paragraph regenerates one chunk, not the book.
Regeneration after a QC failure is `generate(..., force_ids={...}, seed=<new>)`.
"""
from __future__ import annotations

import json
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from chunking.segmenter import Chunk
from tts.client import ElevenLabsClient, VoicePreset


@dataclass
class ChunkArtifact:
    chunk_id: str
    wav: Path
    alignment: Path
    skipped: bool


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


class Generator:
    def __init__(self, client: ElevenLabsClient, preset: VoicePreset, out_dir: Path):
        self.client = client
        self.preset = preset
        self.out_dir = out_dir
        self.manifest_path = out_dir / "manifest.jsonl"
        self._latest = self._load_manifest()

    def _load_manifest(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        if self.manifest_path.exists():
            for line in self.manifest_path.read_text().splitlines():
                if line.strip():
                    entry = json.loads(line)
                    latest[entry["chunk_id"]] = entry
        return latest

    def _archive_previous(self, chunk: Chunk, prev: dict | None) -> str | None:
        """Move the current take to chunks/versions/ before it's replaced, so a regeneration that
        comes out worse can be reverted (`cli.py revert`)."""
        import shutil
        wav_path, align_path = self._paths(chunk)
        if not wav_path.exists():
            return None
        vdir = self.out_dir / "chunks" / "versions"
        vdir.mkdir(parents=True, exist_ok=True)
        tag = (prev or {}).get("generated_at", time.strftime("%Y-%m-%dT%H:%M:%S")).replace(":", "")
        dst = vdir / f"{chunk.id}.{tag}.wav"
        shutil.move(str(wav_path), dst)
        if align_path.exists():
            shutil.move(str(align_path), vdir / f"{chunk.id}.{tag}.alignment.json")
        return str(dst)

    def _paths(self, chunk: Chunk) -> tuple[Path, Path]:
        return self.out_dir / "chunks" / f"{chunk.id}.wav", self.out_dir / "chunks" / f"{chunk.id}.alignment.json"

    def generate(self, chunks: list[Chunk], force_ids: set[str] | None = None,
                 seed: int | None = None, lead_ins: dict[str, str] | None = None,
                 look_aheads: dict[str, str] | None = None) -> list[ChunkArtifact]:
        force_ids = force_ids or set()
        lead_ins = lead_ins or {}
        look_aheads = look_aheads or {}
        fp = self.preset.fingerprint()
        artifacts = []
        for chunk in chunks:
            wav_path, align_path = self._paths(chunk)
            lead = lead_ins.get(chunk.id, "")
            look = look_aheads.get(chunk.id, "")
            h = chunk.content_hash(fp + lead + "\x01" + look)
            prev = self._latest.get(chunk.id)
            if chunk.id not in force_ids and prev and prev.get("hash") == h and wav_path.exists():
                artifacts.append(ChunkArtifact(chunk.id, wav_path, align_path, skipped=True))
                continue

            attempt = (prev or {}).get("attempt", 0) + 1 if prev and prev.get("hash") == h else 1
            t0 = time.time()
            full = lead + chunk.text + look
            result = self.client.generate(full, self.preset, seed=seed)
            sr = self.preset.sample_rate
            pcm, alignment, cut, cut_end = result.pcm, result.alignment, None, None
            if lead or look:
                for extra in range(3):
                    raw = np.frombuffer(result.pcm, dtype=np.int16)
                    write_wav(self.out_dir / "chunks" / f"{chunk.id}.raw.wav", result.pcm, sr)
                    (self.out_dir / "chunks" / f"{chunk.id}.raw.alignment.json").write_text(
                        json.dumps({"text": full, "alignment": result.alignment}, ensure_ascii=False))
                    cut, cut_end = find_cuts(full, len(lead), len(lead) + len(chunk.text), result.alignment or {}, raw, sr)
                    if cut is not None and cut_end is not None:
                        break
                    if extra < 2:
                        print(f"  {chunk.id}: no clean cut point, retrying with a new seed")
                        result = self.client.generate(full, self.preset, seed=(seed or self.preset.seed) + 7 * (extra + 1))
                if cut is None or cut_end is None:
                    print(f"  {chunk.id}: no clean cut point found, regenerating without lead-in/look-ahead")
                    result = self.client.generate(chunk.text, self.preset, seed=seed)
                    pcm, alignment, lead, look, cut, cut_end = result.pcm, result.alignment, "", "", None, None
                else:
                    pcm = raw[int(cut * sr): int(cut_end * sr)].tobytes()
                    alignment = slice_alignment(result.alignment, len(lead), len(lead) + len(chunk.text), cut)
            elapsed = round(time.time() - t0, 2)

            replaced = self._archive_previous(chunk, prev)
            write_wav(wav_path, pcm, self.preset.sample_rate)
            align_path.write_text(json.dumps({
                "text": chunk.text,
                "lead_in": lead,
                "look_ahead": look,
                "cut_start_sec": cut,
                "cut_end_sec": cut_end,
                "alignment": alignment,
                "normalized_alignment": None if lead else result.normalized_alignment,
            }, ensure_ascii=False))

            entry = {
                "chunk_id": chunk.id, "section": chunk.section, "order": chunk.order,
                "boundary_after": chunk.boundary_after, "chars": chunk.chars, "hash": h,
                "preset_fingerprint": fp, "seed": result.seed, "attempt": attempt,
                "request_id": result.request_id, "character_cost": result.character_cost,
                "quality_check": result.quality_check,
                "audio_sec": round(len(pcm) / 2 / self.preset.sample_rate, 3),
                "lead_in_chars": len(lead), "look_ahead_chars": len(look),
                "cut_start_sec": cut, "cut_end_sec": cut_end,
                "elapsed_sec": elapsed, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "replaced_take": replaced,
            }
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with self.manifest_path.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._latest[chunk.id] = entry
            artifacts.append(ChunkArtifact(chunk.id, wav_path, align_path, skipped=False))
            print(f"  generated {chunk.id}: {chunk.chars} chars -> {entry['audio_sec']}s "
                  f"(cost {result.character_cost}, {elapsed}s)")
        return artifacts


def find_cuts(full: str, lo: int, hi: int, alignment: dict, raw: "np.ndarray", sr: int) -> tuple[float | None, float | None]:
    """Start/end cut points (sec) of the real chunk inside a lead-in + chunk + look-ahead
    generation. The alignment only gives an ESTIMATE of each boundary (it runs ~70-140 ms late);
    the cut is the LONGEST real silence in a +-0.5 s window around it — the paragraph break before
    a lead-in/look-ahead is the longest pause there, while "nearest" picked a pause inside the
    chunk's last sentence and dropped its final words ("...of greenspace")."""
    from generation.overlap import silent_run, spoken_char_times
    span = spoken_char_times(full, alignment, lo, hi)
    if span is None:
        return None, None
    c_start, c_end = span
    start, end = 0.0, len(raw) / sr
    if lo > 0:
        prev = spoken_char_times(full, alignment, 0, lo)
        if prev is None:
            return None, None
        r = silent_run(raw, sr, min(prev[1], c_start) - 0.5, max(prev[1], c_start) + 0.5,
                       below_peak_db=50.0, min_run_sec=0.08)
        start = r[0] if r else None
    if hi < len(full):
        nxt = spoken_char_times(full, alignment, hi, len(full))
        if nxt is None:
            return None, None
        r = silent_run(raw, sr, min(c_end, nxt[0]) - 0.5, max(c_end, nxt[0]) + 0.5,
                       below_peak_db=50.0, min_run_sec=0.08)
        end = r[0] if r else None
    if start is None or end is None or end - start < 1.0:
        return None, None
    return start, end


def slice_alignment(alignment: dict, lo: int, hi: int, t0: float) -> dict:
    return {
        "characters": alignment["characters"][lo:hi],
        "character_start_times_seconds": [max(0.0, t - t0) for t in alignment["character_start_times_seconds"][lo:hi]],
        "character_end_times_seconds": [max(0.0, t - t0) for t in alignment["character_end_times_seconds"][lo:hi]],
    }
