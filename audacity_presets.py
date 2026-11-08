"""Lê presets exportados do Audacity (.txt) e converte em filtros do FFmpeg."""
import re

def _num(s):
    return float(s.replace(",", "."))

def parse_audacity(text):
    """Retorna um 'passo' (dict) a partir do texto de um preset do Audacity."""
    text = text.strip().lstrip("\ufeff")
    name, _, rest = text.partition(":")
    kv = dict(re.findall(r'(\w+)="([^"]*)"', rest))
    if name == "BassAndTreble":
        return {"type": "bass_treble", "bass": _num(kv.get("Bass", "0")),
                "treble": _num(kv.get("Treble", "0")), "gain": _num(kv.get("Gain", "0"))}
    if name in ("GraphicEq", "FilterCurve"):
        n = len([k for k in kv if re.fullmatch(r"f\d+", k)])
        pts = sorted((_num(kv[f"f{i}"]), _num(kv[f"v{i}"])) for i in range(n))
        return {"type": "curve", "points": pts}
    raise ValueError(f"Preset não suportado: {name!r}")

def step_to_filter(step):
    if step["type"] == "bass_treble":
        parts = []
        if step["bass"]:
            parts.append(f"bass=g={step['bass']}:f=250")
        if step["treble"]:
            parts.append(f"treble=g={step['treble']}:f=4000")
        if step["gain"]:
            parts.append(f"volume={step['gain']}dB")
        return ",".join(parts)
    if step["type"] == "curve":
        seen, entries = set(), []
        for f, g in step["points"]:
            if f in seen or f <= 0:
                continue
            seen.add(f)
            entries.append(f"entry({f:g},{g:.3f})")
        return "firequalizer=gain_entry='" + ";".join(entries) + "':zero_phase=on"
    raise ValueError(step)

def chain_to_filter(chain, limiter=True):
    fl = [f for f in (step_to_filter(s) for s in chain) if f]
    if limiter:  # evita estourar o áudio depois de tanto ganho
        fl.append("alimiter=limit=0.95")
    return ",".join(fl)


def describe_step(step):
    """Resumo curto de um passo, pra mostrar nos embeds."""
    if step["type"] == "bass_treble":
        return f"🎚️ Graves {step['bass']:+g} · Agudos {step['treble']:+g} · Ganho {step['gain']:+g} dB"
    n = len(step["points"])
    return f"📈 Curva de EQ ({n} ponto{'s' if n != 1 else ''})"
