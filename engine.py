"""Motor de áudio: graves/agudos via FFmpeg; curvas de EQ (gráfico/filter curve) via numpy/scipy.

As curvas são feitas em Python porque o filtro firequalizer do FFmpeg falhou por falta de memória
no servidor. Aqui usamos um filtro FIR de fase linear (8191 pontos, igual ao Audacity).
"""
import numpy as np
from scipy.signal import oaconvolve

from audacity_presets import step_to_filter

SR = 44100
FIR_LEN = 8191
N_FFT = 16384


def plan(chain):
    """Agrupa passos consecutivos do mesmo tipo: [('ff', [passos]), ('py', [curvas]), ...]."""
    groups = []
    for step in chain:
        kind = "ff" if step["type"] == "bass_treble" else "py"
        if groups and groups[-1][0] == kind:
            groups[-1][1].append(step)
        else:
            groups.append((kind, [step]))
    return groups


def ff_filter(steps):
    return ",".join(f for f in (step_to_filter(s) for s in steps) if f)


def design_fir(curves):
    """Combina várias curvas (soma em dB) num único FIR de fase linear."""
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    logf = np.log10(np.maximum(freqs, 1.0))
    total_db = np.zeros_like(freqs)
    for c in curves:
        pts = np.array(c["points"], dtype=float)
        pts = pts[pts[:, 0] > 0]
        total_db += np.interp(logf, np.log10(pts[:, 0]), pts[:, 1])  # fora das pontas: constante
    mag = 10 ** (total_db / 20)
    h = np.fft.fftshift(np.fft.irfft(mag, N_FFT))
    mid = N_FFT // 2
    half = FIR_LEN // 2
    h = h[mid - half: mid + half + 1] * np.blackman(FIR_LEN)
    return h.astype(np.float32)


def apply_curves(raw_in, raw_out, curves):
    """Lê PCM float32 estéreo, aplica o FIR (sem atraso) e grava de novo."""
    x = np.fromfile(raw_in, dtype="<f4").reshape(-1, 2)
    h = design_fir(curves)
    y = np.empty_like(x)
    for ch in range(2):
        y[:, ch] = oaconvolve(x[:, ch], h, mode="same")
    y.astype("<f4").tofile(raw_out)
