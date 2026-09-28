"""
Pronunciation check reel: every candidate term (from the direction pass) spoken in its real
sentence from the book, one after another, plus a numbered index with timestamps — so a human
reviews ~16 short items instead of hunting through 20 minutes of narration.

Each clip is also transcribed; what the ASR heard is shown next to the term as a hint (the ASR is
not the judge — the listener is).
"""
from __future__ import annotations

import json
import re
import wave
from pathlib import Path

import numpy as np

GAP_SEC = 1.5
_SENT = re.compile(r"(?<=[.!?।])\s+")


def _sentence_with(term: str, texts: list[str]) -> str | None:
    t = term.lower()
    for text in texts:
        for s in _SENT.split(re.sub(r"\[[^\]]*\]", " ", text)):
            if t in s.lower():
                s = re.sub(r"\s+", " ", s).strip()
                return s if len(s) <= 300 else s[: s.lower().find(t) + len(t) + 80]
    return None


def build_reel(work_dir: Path, client, preset, qc, out_name: str = "pronunciation_check") -> dict:
    lang = preset.language_code
    direction = json.loads((work_dir / "direction.json").read_text())
    texts = [c["text"] for c in json.loads((work_dir / "chunks.json").read_text())["chunks"]]
    title = json.loads((work_dir / "chunks.json").read_text())
    carrier = {"en": "This summary is based on the book by {t}.", "hi": "यह सारांश {t} की किताब पर आधारित है।"}

    out_dir = work_dir / out_name
    out_dir.mkdir(exist_ok=True)
    sr = preset.sample_rate
    pieces, index, t = [np.zeros(int(sr * 0.5), dtype=np.int16)], [], 0.5
    for n, cand in enumerate(direction["pronunciation_candidates"], 1):
        term = cand["term"]
        sentence = _sentence_with(term, texts) or carrier[lang].format(t=term)
        clip = out_dir / f"{n:02d}.wav"
        if not clip.exists():
            res = client.generate(sentence, preset)
            with wave.open(str(clip), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(res.pcm)
        with wave.open(str(clip)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        heard = qc.transcribe(clip, lang)["text"]
        index.append({"n": n, "at": f"{int(t // 60)}:{t % 60:04.1f}", "term": term,
                      "sentence": sentence, "asr_heard": heard, "why": cand.get("why"),
                      "suggested_fix": cand.get("suggestion")})
        pieces += [pcm, np.zeros(int(sr * GAP_SEC), dtype=np.int16)]
        t += len(pcm) / sr + GAP_SEC

    reel = work_dir / f"{out_name}_{lang.upper()}.wav"
    with wave.open(str(reel), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(np.concatenate(pieces).tobytes())
    (work_dir / f"{out_name}_{lang.upper()}.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    return {"reel": reel, "index": index, "title": title.get("title")}
