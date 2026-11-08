import asyncio, io, json, os, shutil, tempfile
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands

from audacity_presets import describe_step, parse_audacity
from engine import apply_curves, ff_filter, plan

BASE = Path(__file__).parent
DEFAULT_PRESETS = BASE / "presets.default.json"
PRESETS_FILE = Path(os.getenv("PRESETS_PATH", BASE / "presets.json"))  # em nuvem: aponte pro volume (ex: /data/presets.json)
DEFAULT_LIMIT = 10 * 1024 * 1024
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".wma"}

ROXO, VERDE, VERMELHO, AMARELO = 0x7B5CFF, 0x2ECC71, 0xE74C3C, 0xF1C40F


def emb(titulo, desc=None, cor=ROXO):
    e = discord.Embed(title=titulo, description=desc, color=cor)
    e.set_footer(text="🎧 Audio Studio • feito pro MTA")
    return e


def erro(msg):
    return emb("❌ Opa, deu ruim", msg, VERMELHO)


# ---------- presets ----------
def load_presets():
    if not PRESETS_FILE.exists():
        PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DEFAULT_PRESETS.exists():
            shutil.copy(DEFAULT_PRESETS, PRESETS_FILE)
        else:
            return {}
    return json.loads(PRESETS_FILE.read_text(encoding="utf8"))


def save_presets(p):
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(p, ensure_ascii=False), encoding="utf8")


async def ler_passos(anexo):
    """Aceita .txt do Audacity ou .zip com vários .txt (ordem alfabética: 1º, 2º...)."""
    dados = await anexo.read()
    if anexo.filename.lower().endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(io.BytesIO(dados)) as z:
            nomes = sorted(n for n in z.namelist() if n.lower().endswith(".txt") and "leia" not in n.lower())
            return [parse_audacity(z.read(n).decode("utf8", errors="replace")) for n in nomes]
    return [parse_audacity(dados.decode("utf8", errors="replace"))]


async def preset_autocomplete(inter: discord.Interaction, atual: str):
    return [app_commands.Choice(name=k, value=k) for k in load_presets() if atual.lower() in k.lower()][:25]


def eh_audio(a: discord.Attachment):
    return (a.content_type or "").startswith("audio") or Path(a.filename).suffix.lower() in AUDIO_EXT


# ---------- ffmpeg ----------
async def ffmpeg(etapa, *args):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-filter_threads", "1", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip()
        if len(msg) > 900:
            msg = msg[:450] + "\n...\n" + msg[-450:]
        raise RuntimeError(f"[{etapa}] código {proc.returncode}\n{msg}")


RAW = ("-f", "f32le", "-ac", "2", "-ar", "44100")  # PCM float32 estéreo 44.1 kHz entre as etapas


async def run_edit(inter, arquivo, chain, sufixo, titulo, detalhes, trim=(), extra_af=""):
    """A interação precisa já ter respondido (mensagem 'processando'); aqui só editamos ela."""
    limit = inter.guild.filesize_limit if inter.guild else DEFAULT_LIMIT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "in"
            await arquivo.save(src)
            # etapa 1: decodifica qualquer formato pra PCM float 44.1 kHz estéreo
            cur = tmp / "s0.raw"
            await ffmpeg("leitura", *trim, "-threads", "1", "-i", str(src), "-vn", *RAW, str(cur))
            # etapa 2: passos do preset (graves/agudos no FFmpeg, curvas de EQ em Python)
            for i, (kind, steps) in enumerate(plan(chain), 1):
                nxt = tmp / f"s{i}.raw"
                if kind == "ff":
                    await ffmpeg("graves/agudos", "-threads", "1", *RAW, "-i", str(cur),
                                 "-af", ff_filter(steps), *RAW, str(nxt))
                else:
                    await asyncio.to_thread(apply_curves, cur, nxt, steps)
                cur.unlink(missing_ok=True)
                cur = nxt
            # etapa 3: limitador (evita estourar) + MP3
            af = ",".join(f for f in (extra_af, "alimiter=limit=0.95" if chain else "") if f)
            nome = Path(arquivo.filename).stem + f"_{sufixo}.mp3"
            out = tmp / nome
            for bitrate in ("192k", "128k", "96k"):
                await ffmpeg("mp3", "-threads", "1", *RAW, "-i", str(cur), *(("-af", af) if af else ()),
                             "-threads", "1", "-b:a", bitrate, str(out))
                if out.stat().st_size <= limit:
                    break
            else:
                return await inter.edit_original_response(
                    embed=erro("Ficou grande demais pro Discord, mesmo comprimindo. Tenta uma música menor."), view=None)
            mb = out.stat().st_size / 1024 / 1024
            e = emb(f"✅ {titulo}", f"**{arquivo.filename}**", VERDE)
            e.add_field(name="Detalhes", value=detalhes, inline=False)
            e.add_field(name="Qualidade", value=f"MP3 {bitrate}", inline=True)
            e.add_field(name="Tamanho", value=f"{mb:.1f} MB", inline=True)
            msg = await inter.edit_original_response(embed=e, attachments=[discord.File(out, nome)], view=None)
        url = msg.attachments[0].url
        e.add_field(name="🔗 Link pro servidor", value=f"```{url}```", inline=False)
        await inter.edit_original_response(embed=e, view=ResultView(arquivo, inter.user.id, url))
    except Exception as ex:
        info = f"Arquivo: `{arquivo.filename}` ({arquivo.size / 1024 / 1024:.1f} MB)\n```{str(ex)[:900]}```"
        await inter.edit_original_response(embed=erro(info), view=None, attachments=[])


def processando(txt="Processando seu áudio..."):
    return emb("⏳ Trabalhando nisso", f"{txt}\nIsso leva alguns segundos.", AMARELO)


# ---------- views (menus e botões) ----------
class PresetSelectView(discord.ui.View):
    def __init__(self, arquivo, autor_id):
        super().__init__(timeout=120)
        self.arquivo, self.autor_id = arquivo, autor_id
        presets = load_presets()
        opts = [discord.SelectOption(label=k[:100], value=k, description=f"{len(v)} passo(s)", emoji="🎛️")
                for k, v in list(presets.items())[:25]]
        sel = discord.ui.Select(placeholder="Escolha o preset...", options=opts)
        sel.callback = self.escolheu
        self.add_item(sel)

    async def interaction_check(self, inter):
        if inter.user.id != self.autor_id:
            await inter.response.send_message("Esse menu é de outra pessoa 😅", ephemeral=True)
            return False
        return True

    async def escolheu(self, inter: discord.Interaction):
        nome = inter.data["values"][0]
        chain = load_presets().get(nome)
        if not chain:
            return await inter.response.edit_message(embed=erro(f"Preset `{nome}` não existe mais."), view=None)
        await inter.response.edit_message(embed=processando(f"Aplicando **{nome}**..."), view=None)
        det = "\n".join(f"`{i}.` {describe_step(s)}" for i, s in enumerate(chain, 1))
        await run_edit(inter, self.arquivo, chain, nome, "Música pronta", f"**Preset:** {nome}\n{det}")


class ResultView(discord.ui.View):
    def __init__(self, arquivo, autor_id, url):
        super().__init__(timeout=300)
        self.arquivo, self.autor_id = arquivo, autor_id
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Abrir link", emoji="🔗", url=url))

    @discord.ui.button(label="Outro preset", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def outro(self, inter: discord.Interaction, _):
        if inter.user.id != self.autor_id:
            return await inter.response.send_message("Só quem pediu pode refazer 😅", ephemeral=True)
        await inter.response.edit_message(
            embed=emb("🎛️ Escolha o preset", f"Refazendo **{self.arquivo.filename}** a partir do original."),
            attachments=[], view=PresetSelectView(self.arquivo, self.autor_id))


class ConfirmarApagar(discord.ui.View):
    def __init__(self, nome):
        super().__init__(timeout=30)
        self.nome = nome

    @discord.ui.button(label="Apagar", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def sim(self, inter: discord.Interaction, _):
        p = load_presets(); p.pop(self.nome, None); save_presets(p)
        await inter.response.edit_message(embed=emb("🗑️ Apagado", f"Preset `{self.nome}` removido.", VERDE), view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def nao(self, inter: discord.Interaction, _):
        await inter.response.edit_message(embed=emb("Cancelado", "Nada foi apagado."), view=None)


# ---------- bot ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(),
                         activity=discord.Activity(type=discord.ActivityType.listening, name="/editar"))
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        if os.getenv("PORT"):  # Render (web service) exige uma porta aberta
            app = web.Application()
            app.router.add_get("/", lambda r: web.Response(text="ok"))
            runner = web.AppRunner(app); await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", int(os.environ["PORT"])).start()

    async def on_ready(self):
        print("Bot online:", self.user)


client = Bot()
tree = client.tree


@tree.command(name="editar", description="Aplica um preset na sua música")
@app_commands.describe(arquivo="Música (mp3, wav, ogg...)", preset="Preset (deixe vazio pra escolher num menu)")
@app_commands.autocomplete(preset=preset_autocomplete)
async def editar(inter: discord.Interaction, arquivo: discord.Attachment, preset: str = None):
    if not eh_audio(arquivo):
        return await inter.response.send_message(embed=erro("Isso não parece um arquivo de áudio."), ephemeral=True)
    presets = load_presets()
    if not presets:
        return await inter.response.send_message(embed=erro("Nenhum preset salvo. Use `/preset_importar`."), ephemeral=True)
    if preset is None:
        return await inter.response.send_message(
            embed=emb("🎛️ Escolha o preset", f"Música: **{arquivo.filename}**"),
            view=PresetSelectView(arquivo, inter.user.id))
    chain = presets.get(preset)
    if not chain:
        return await inter.response.send_message(embed=erro(f"Preset `{preset}` não existe. Use `/presets`."), ephemeral=True)
    await inter.response.send_message(embed=processando(f"Aplicando **{preset}**..."))
    det = "\n".join(f"`{i}.` {describe_step(s)}" for i, s in enumerate(chain, 1))
    await run_edit(inter, arquivo, chain, preset, "Música pronta", f"**Preset:** {preset}\n{det}")


@tree.command(name="cortar", description="Corta um trecho da música (em segundos) com fade")
@app_commands.describe(inicio="Segundo inicial", fim="Segundo final", fade="Duração do fade in/out (s)")
async def cortar(inter: discord.Interaction, arquivo: discord.Attachment, inicio: float, fim: float, fade: float = 1.0):
    if not eh_audio(arquivo):
        return await inter.response.send_message(embed=erro("Isso não parece um arquivo de áudio."), ephemeral=True)
    if inicio < 0 or fim <= inicio:
        return await inter.response.send_message(embed=erro("O final precisa ser maior que o início."), ephemeral=True)
    dur = fim - inicio
    fade = min(fade, dur / 2)
    f = f"afade=t=in:d={fade},afade=t=out:st={dur - fade}:d={fade}"
    await inter.response.send_message(embed=processando("Cortando..."))
    await run_edit(inter, arquivo, [], "corte", "Corte pronto",
                   f"✂️ {inicio:g}s → {fim:g}s ({dur:g}s)\n🌅 Fade de {fade:g}s",
                   trim=("-ss", str(inicio), "-t", str(dur)), extra_af=f)


@tree.command(name="preset_importar", description="Cria um preset a partir de presets do Audacity (.txt ou .zip)")
@app_commands.describe(nome="Nome do preset novo", arquivo1=".txt ou .zip", arquivo2="2º .txt (opcional)",
                       arquivo3="3º .txt (opcional)", arquivo4="4º .txt (opcional)",
                       arquivo5="5º .txt (opcional)", arquivo6="6º .txt (opcional)")
async def preset_importar(inter: discord.Interaction, nome: str, arquivo1: discord.Attachment,
                          arquivo2: discord.Attachment = None, arquivo3: discord.Attachment = None,
                          arquivo4: discord.Attachment = None, arquivo5: discord.Attachment = None,
                          arquivo6: discord.Attachment = None):
    await inter.response.defer(ephemeral=True)
    try:
        chain = []
        for a in (arquivo1, arquivo2, arquivo3, arquivo4, arquivo5, arquivo6):
            if a:
                chain += await ler_passos(a)
        if not chain:
            return await inter.followup.send(embed=erro("Não achei nenhum preset válido nos arquivos."))
        p = load_presets(); p[nome] = chain; save_presets(p)
        e = emb("💾 Preset salvo", f"**{nome}** — {len(chain)} passo(s)", VERDE)
        e.add_field(name="Passos", value="\n".join(f"`{i}.` {describe_step(s)}" for i, s in enumerate(chain, 1))[:1000])
        e.add_field(name="Como usar", value=f"`/editar` e escolha **{nome}**", inline=False)
        await inter.followup.send(embed=e)
    except Exception as ex:
        await inter.followup.send(embed=erro(f"Não consegui ler o arquivo.\n```{ex}```"))


@tree.command(name="preset_adicionar", description="Adiciona passos do Audacity no fim de um preset")
@app_commands.autocomplete(nome=preset_autocomplete)
async def preset_adicionar(inter: discord.Interaction, nome: str, arquivo: discord.Attachment):
    await inter.response.defer(ephemeral=True)
    p = load_presets()
    if nome not in p:
        return await inter.followup.send(embed=erro(f"Preset `{nome}` não existe."))
    try:
        p[nome] += await ler_passos(arquivo); save_presets(p)
        await inter.followup.send(embed=emb("➕ Passo adicionado", f"**{nome}** agora tem {len(p[nome])} passo(s).", VERDE))
    except Exception as ex:
        await inter.followup.send(embed=erro(f"Não consegui ler o arquivo.\n```{ex}```"))


@tree.command(name="preset_apagar", description="Apaga um preset salvo")
@app_commands.autocomplete(nome=preset_autocomplete)
async def preset_apagar(inter: discord.Interaction, nome: str):
    if nome not in load_presets():
        return await inter.response.send_message(embed=erro(f"Preset `{nome}` não existe."), ephemeral=True)
    await inter.response.send_message(embed=emb("🗑️ Apagar preset?", f"Tem certeza que quer apagar **{nome}**?", AMARELO),
                                      view=ConfirmarApagar(nome), ephemeral=True)


@tree.command(name="presets", description="Mostra os presets salvos")
async def presets(inter: discord.Interaction):
    p = load_presets()
    if not p:
        return await inter.response.send_message(embed=erro("Nenhum preset salvo ainda."), ephemeral=True)
    e = emb("🎛️ Seus presets", "Use `/editar` pra aplicar.")
    for nome, chain in list(p.items())[:25]:
        e.add_field(name=nome, value="\n".join(f"`{i}.` {describe_step(s)}" for i, s in enumerate(chain, 1))[:1000], inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)


@tree.command(name="backup", description="Baixa um backup dos seus presets (.json)")
async def backup(inter: discord.Interaction):
    data = json.dumps(load_presets(), ensure_ascii=False).encode("utf8")
    await inter.response.send_message(embed=emb("📦 Backup dos presets", "Guarde esse arquivo. Pra restaurar: `/restaurar`."),
                                      file=discord.File(io.BytesIO(data), "presets_backup.json"), ephemeral=True)


@tree.command(name="restaurar", description="Restaura presets de um backup (.json)")
async def restaurar(inter: discord.Interaction, arquivo: discord.Attachment):
    await inter.response.defer(ephemeral=True)
    try:
        novo = json.loads((await arquivo.read()).decode("utf8"))
        assert isinstance(novo, dict) and all(isinstance(v, list) for v in novo.values())
        p = load_presets(); p.update(novo); save_presets(p)
        await inter.followup.send(embed=emb("♻️ Restaurado", f"{len(novo)} preset(s) importado(s).", VERDE))
    except Exception:
        await inter.followup.send(embed=erro("Esse não parece um backup válido."))


@tree.command(name="ajuda", description="Mostra os comandos do bot")
async def ajuda(inter: discord.Interaction):
    e = emb("🎧 Audio Studio", "Edito suas músicas com presets do Audacity e devolvo com link pro servidor.")
    e.add_field(name="🎵 Editar", value="`/editar` aplica um preset\n`/cortar` corta um trecho com fade", inline=False)
    e.add_field(name="🎛️ Presets", value="`/preset_importar` `/preset_adicionar`\n`/preset_apagar` `/presets`", inline=False)
    e.add_field(name="📦 Backup", value="`/backup` e `/restaurar`", inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)


client.run(os.environ["DISCORD_TOKEN"])
