# WHERE_TO_TUNE.md — vilken fil styr vad

Snabbkarta för effekter och ljud. Grundregeln i hela projektet:
**`pacing.py` BESTÄMMER, `render.py`/`sfx.py` UTFÖR, `config.py` har talen.**
Bearbetar du logiken i `orchestrator.py` är du fel — den bara kopplar ihop.

---

## 1. Bild-effekter (zoom, skak, flash, glitch)

| Fil | Roll | Vad du ändrar här |
|-----|------|-------------------|
| **`pacing.py`** | **BESTÄMMER** vilken effekt en beat får, och NÄR | `decide_effect()` — reglerna för zoom_punch / shake / flash / glitch / fast_cut / slide |
| **`render.py`** | **UTFÖR** effekten som ffmpeg-filter | `render_segment()` rad ~78-108: crop-uttrycken, `eq=brightness`, `hue=`, `fade=` |
| **`config.py`** | Talen | `CUT_MIN_SEC`/`CUT_MAX_SEC` (2.2–3.6 s), `HOOK_MAX_SEC` (3.0), `PUNCH_WORDS`, `RANDOM_SEED` |

```
pacing.py:  z = rms_z(...)  +  punchword  ->  decide_effect()  ->  b.effect = "zoom_punch"
render.py:  if effect == "zoom_punch":  crop=...  zoom in mot ansiktet
```

Vill du ha **lugnare klippning**: höj `CUT_MIN_SEC`/`CUT_MAX_SEC` i `config.py`.
Vill du ha **andra effektregler** (t.ex. aldrig glitch): ändra `decide_effect()` i `pacing.py`.
Vill du ha **hårdare flash**: ändra `0.35` i `eq=brightness` i `render.py`.

---

## 2. "Pling"-ljuden (SFX)

| Fil | Roll | Vad du ändrar här |
|-----|------|-------------------|
| **`sfx.py`** | **BYGGER** ljuden (syntes, ingen ljudfil behövs) | `hit()`, `riser()`, `sub_drop()`, `braaam()`, `whoosh()`, `glitch_tick()`, `ui_click()` — rad 80-148 |
| **`sfx.py`** | **PLACERAR** dem på tidslinjen | `hit_events_for()` rad ~215 — kopplar beat-effekt -> ljud + volym i dB |
| **`sfx.py`** | **MIXAR** dem till ett spår | `build_bed()` |
| **`render.py`** | Lägger spåret i slutljudet | `mix_final()` — `volume={config.SFX_VOLUME}` |
| **`config.py`** | Rattarna | `SFX_ENABLED`, `SFX_VOLUME` (0.85), `MUSIC_VOLUME` (0.30), `NARRATION_VOLUME` (1.7), **`SFX_MIN_GAP` (6.0), `SFX_MAX_PER_VIDEO` (6)** |

### Hur många träffar som hörs (det var här pling-spammen satt)

`hit_events_for()` samlar först **kandidater** med en prioritet (shake 3, zoom_punch 2,
flash/glitch 1, payoff-riser 2), sorterar på prioritet och släpper sedan igenom max
`SFX_MAX_PER_VIDEO` stycken, aldrig två närmare än `SFX_MIN_GAP` sekunder.
Outro-stingen (sub_drop + braaam) ligger utanför budgeten — den är strukturell.

Förut small `hit` på **varje** `zoom_punch`, vilket är den vanligaste effekten: en 48 s
short fick ~15 träffar, alltså en varannan sekund. Nu blir det 5-6.
Vill du ha fler: höj `SFX_MAX_PER_VIDEO` och sänk `SFX_MIN_GAP`. Sätt `SFX_MIN_GAP=0`
för gamla beteendet.

Loggen skriver både planen och **vad som faktiskt ligger i bädden**:
`[sfx] 6 impacts planned, 4 dropped by the 6s gap` + `[sfx] bed measured: 2 impacts, one every 34.1s`.

### Vilket ljud hörs när

| Bild-händelse | Ljud | Volym |
|---|---|---|
| `zoom_punch` eller `shake` | **hit** (det du kallar plinget) | −11 dB |
| `flash` eller `glitch` | glitch_tick | −16 dB |
| intent `shock`/`payoff` | riser (2.4 s innan) | −17 dB |
| outron | sub_drop + braaam | −12 / −10 dB |

### Så här låter ett ljud (hit = "plinget")

```python
def hit(dur=0.7):
    thump = sin(2*pi*82*t)  * exp(-t*13)    # låg dunk
    click = noise           * exp(-t*90)    # klick-transient
    body  = sin(2*pi*190*t) * exp(-t*26)    # kropp
```

Vill du ha **ljusare pling**: höj `190.0` mot t.ex. `520.0`.
Vill du ha **kortare**: sänk `dur=0.7`.
Vill du ha **högre**: ändra `-11.0` i `hit_events_for()`.

Ljuden skrivs till `assets/sfx/*.wav` första gången och återanvänds.
De är git-ignorerade — kör `python sfx.py` för att bygga om dem.

---

## 3. Musik (inte SFX, men hänger ihop)

**`music.py`** — `STYLES`: `intro` 92 / `rising` 110 / `climax` 126 / `outro` 92 BPM,
plus **`mysterious`** som inte är en beat-bädd alls: en kontinuerlig dron på 55 Hz med
långsam svävning (0.6 Hz slag mellan två stämda sinnen), en kvint över, andning på
0.07 Hz och ett moll-sväll var 9:e sekund. **Ingen percussion.** Därför kan den ligga
under tal i flera minuter utan att konkurrera.

| Ratt | Värde | Betydelse |
|---|---|---|
| `MUSIC_STYLE` | `"mysterious"` | vilken stil körningen använder |
| `MUSIC_VOLUME` | 0.30 | drone tål mer än man tror; en beat-bädd måste ligga lägre |
| `MUSIC_STYLE="climax"` | — | tillbaka till beat-bädd för shorts |

Loggen mäter bädden: `[music] ... (52s, 0 attacks/min - a beat bed measures ~120)`.
Noll attacker = ingen percussion, alltså en drone. Mätningen ligger i
`music.bed_stats()`.

---

## 2b. DIALOGSTRUKTUREN — vem som pratar när (den viktigaste ratten)

Grundregeln: **TTS och personens röst ligger aldrig samtidigt.** Tidslinjen delas i
separata segment, inte i volymnivåer. Se `dialogue.py`.

```
[SHOW] personen pratar, original-ljud, captions för hens ord      (gul text)
[TELL] berättaren förklarar, klippet är TYST, captions för manuset (vit text)
[SHOW] nästa klipp ...
[TELL] nästa förklaring ...
[OUTRO] kortet + uppläst CTA
```

Varför det inte kan läcka: varje segment renderas till sin egen fil, och ljudet byggs
som **ett spår** i `dialogue.build_track()` där varje segments ljud kopieras in i sitt
eget tidsfönster. Berättaren finns bara i TELL-fönster, personen bara i SHOW-fönster.

| Ratt | Fil | Betydelse |
|---|---|---|
| `DIALOGUE_MODE` | `config.py` | True = växelspel. False = gamla voiceover-beteendet |
| `SHOW_SHARE` | `config.py` | andel av speltiden klippen får (0.34). **Höj = kortare manus, mer person** |
| `SHOW_MIN_SEC` / `SHOW_MAX_SEC` | `config.py` | 1.8 / 6.0 s per klippsegment |
| `DIALOGUE_SOURCE_GAIN` | `config.py` | 1.0 — klippets ljud ligger ensamt i sitt fönster |
| `SOURCE_ASR_MODEL` | `config.py` | whisper-modell för klippens textning (`base`) |

Antalet SHOW-segment **härleds ur tidsbudgeten**, inte ur antal manusrader: med 45 s mål
och 30 s narration finns 10 s kvar, alltså ~5 klipp à 2 s. Ett klipp per rad hade gett
64 s video och underkänts av längdgrinden.

Klippens fönster väljs ur **detekterat tal** (`dialogue.speech_spans`, energibaserad VAD),
inte genom aritmetik på tiden — ett SHOW-segment måste landa där någon faktiskt pratar.
TELL-segmentens bild tas från tysta partier, så man slipper se någon röra munnen i tysthet.

```bash
python dialogue.py clip work/clips/c001.mp4     # visa tal-spann + transkript
python dialogue_selftest.py nagon_video.mp4     # bevisa växelspelet på riktigt
```

Självtestet mäter: rätt ljud i varje segment (0.85–0.94 korrelation), ingen blödning
(två olika röster korrelerar ~0.2 av en slump), att de två caption-spåren aldrig
överlappar, och att outron inte är tyst.

### Så mäts växelspelet på en färdig fil (`dialogue.verify`)

`verify` jämför den **färdiga** filens ljud mot vad varje fönster ska innehålla:
ett TELL-fönster mot sin narrationsrad, ett SHOW-fönster mot klippets eget ljud.
Bara energins form jämförs (korrelation av enveloper), för filen har gått genom
AAC, musikbädd, limiter och loudnorm — samplen finns inte kvar, men *när* energin
kommer gör det.

Två fel som gjorde att den underkände en korrekt 5-minutersfilm, och som är
rättade:

- **SHOW-referensen togs exakt vid `src_in`.** Bilden är frame-kvantiserad
  (29.97 fps) och ett muxat klipp kan ha en ljudfördröjning, så ett fönster med
  rätt ljud kunde ligga en eller två frames fel och få 0.46 mot referensen —
  medan samma ljud 50 ms bort gav 0.75. Referensen söks nu i ett litet område
  runt `src_in` (±0.4 s, steg 20 ms), och **hur mycket den flyttades skrivs ut**
  i kolumnen `src shift` (typiskt 0.00–0.15 s).
- **"Bleed" jämfördes mot varje rad som ens tangerade fönstret**, inklusive den
  som börjar exakt där fönstret slutar. Noll överlapp betydde att hela fönstret
  jämfördes med början av nästa rad — alltså orelaterat tal, som råkade ge 0.61
  och fällde filmen. Blödning mäts nu **bara där raderna verkligen överlappar**
  (minst 0.25 s).

Dessutom kontrolleras 8 fönster utspridda över **hela** filen, inte bara öppningen
(förr: de 8 första, vilket på en 5-minutersfilm betydde de första 18 sekunderna).

Nivåkolumnen (`level`, dB mot det byggda spårets rms) skrivs ut men avgör inte
godkännandet: musikbädden lyfter tysta fönster, så avvikelsen är till för att
läsas. En riktig körning visar personens fönster på 0.025–0.073 i rms mot
berättarens 0.081–0.106 — personen ligger alltid under berättaren, aldrig över.
Det är samma sak som att inget tal ligger ovanpå.

---

## 2c. Mixen: varför man hör personen i klippet

`render.py:mix_final()` — fyra ingångar: klippets eget ljud, berättaren, musik, SFX.

```
[0:a] volume=SOURCE_AUDIO_VOLUME, asplit -> [src] + [key]
[1:a] volume=NARRATION_VOLUME -> [nraw]
[nraw][key] sidechaincompress -> [n]        <-- duckning
[n][src][m][s] amix weights='1.0 1.0 0.6 0.5' -> apad -> atrim -> alimiter -> loudnorm
```

| Ratt | Värde | Betydelse |
|---|---|---|
| `SOURCE_AUDIO_VOLUME` | 0.55 | klippets eget ljud. **0.18 var buggen** — gånger amix-vikten 0.6 blev det 0.108 mot berättarens 1.7, alltså ~6 %, det vill säga ohörbart |
| `DUCK_ENABLED` | True | berättaren backar när klippet pratar |
| `DUCK_RATIO` | 5.0 | 5:1 ger ~8 dB sänkning, mätt |
| `DUCK_THRESHOLD` | 0.045 | var tröskeln ligger för vad som räknas som "någon pratar" |
| `DUCK_ATTACK/RELEASE` | 25/600 ms | snabb ned, långsam upp = pumpar inte mellan ord |

Vill du att berättaren tar mer plats: sänk `DUCK_RATIO` till 3.
Vill du att klippet hörs ännu mer: höj `SOURCE_AUDIO_VOLUME` mot 0.7.
Stäng av hela duckningen: `DUCK_ENABLED = False`.

`apad` + `atrim=0:<videons längd>` ser till att ljudet alltid täcker hela videon.
Förut kapades det till narrationens längd, vilket gjorde de sista ~5 sekunderna —
outro-kortet — helt tysta.

---

## 3b. Ljudnivån i den färdiga filen (-14 LUFS)

Levererad fil ska ligga på **-14 LUFS** med topp under -1 dBTP, det är där YouTube
slutar sänka volymen. Det gick inte med ett enkelt `loudnorm`-pass:

- `loudnorm` i ett pass gav **-16.1 LUFS** på en riktig kort-film (mätt med
  `audio_check.py`), alltså 2 dB för tyst.
- Tvåpass-varianten (`measured_I=...:linear=true`) gav **-15.7 LUFS**. Orsaken är
  att linear-loudnorm klampar sin vinst för att hålla true peak under taket:
  mixen behövde **+6.1 dB** men topparna låg på **-5.9 dBTP**, så vinsten kapades.

Så här gör `render.mix_dialogue` i stället, vilket är det som gäller:

1. Mixa dialog + musik + SFX till en tillfällig wav (pass 1) och **mät** den.
2. Pass 2 = `volume=<target - uppmätt>dB` följt av `alimiter=limit=0.80`.
   Vinsten appliceras rakt av, limitern tar bara de toppar som går över taket.
3. Mät den **levererade filen** och rätta resten i upp till tre små pass
   (`volume=<rest>dB,alimiter`), tills |rest| <= 0.3 LU.

Resultat: -14.1 LUFS / -1.7 dBTP på ett avsiktligt elakt testklipp, -14.5 på
nästa, och samma sak på riktiga kort-filmen. Taket 0.80 och inte 0.891: vid 0.891
mätte AAC-kodningen **+0.0 dBTP** efter encodern, alltså uppe på taket.

**Nivån är fel i en färdig fil** -> titta efter raden `[mix] delivered ... LUFS`
i körloggen. Saknas den är `_loudnorm_measure` trasig (den läser JSON från
`loudnorm=print_format=json`), och då hoppas korrigeringen över.
**Filmen blev tystare än -14 trots fixen** -> höj inte `MUSIC_VOLUME`; kolla
först `alimiter`-taket, för det är limitern som äter vinsten när materialet har
många toppar.

## 4. Grafiken (tweet-kort, lower thirds, stat-kort)

| Fil | Roll |
|-----|------|
| **`cues.py`** | **BESTÄMMER** vilken grafik en replik får (person -> lower_third, social -> tweet_ui, siffra -> stat_card) |
| **`gfx.py`** | **RITAR** korten (14 byggare: lower_third, tweet_ui, stat_card, timeline_card, bar_chart, …) |
| **`config.py`** | `CUES_ENABLED`, `CUE_MAX_PER_VIDEO`, `ATMOSPHERE_ENABLED` |
| **`assets/fonts/`** | Anton, Archivo Black, Bebas Neue (OFL) |

---

## 5. Film-look och atmosfär

| Fil | Roll |
|-----|------|
| **`cinema.py`** | `GRADE` (teal/orange), `GRAIN`, `VIGNETTE` — strängar som läggs i en enda ffmpeg-pass |
| **`luts.py`** | Genererar en `.cube`-LUT som ersätter `GRADE` (`config.USE_LUT`) |
| **`cues.py`** | `atm_flare.png`, `atm_leak.png`, `atm_hud.png` — atmosfärslagren |

---

## 6. Verktyg som mäter i stället för att tycka

| Kommando | Vad det svarar på |
|---|---|
| `python audio_check.py output/<fil>.mp4` | längd, LUFS, true peak, LRA, transient-täthet, talbandsnivå |
| `python audio_check.py --bed work/sfx.wav` | **exakt** antal träffar och mellanrum i SFX-bädden (ingen speech som stör) |
| `python -m verify output/<fil>.mp4` | anti-slideshow-gaten (aktivitetsmedian, ljudtoppar, längd) |
| `python main.py --list` | alla färdiga filer med längd, storlek, upplösning |
| `python fallback_script.py "Tema" --words 660` | skriptet som skrivs när ingen LLM-nyckel finns |

---

## Snabb felsökning

**"Man hör inte personen i klippet"** -> `config.SOURCE_AUDIO_VOLUME` upp, och kontrollera
att `DUCK_ENABLED` är True. Det var hela buggen en gång: 0.18 × amix-vikten 0.6.
**"Pling hela tiden"** -> `config.SFX_MAX_PER_VIDEO` ner / `SFX_MIN_GAP` upp, eller ta bort
`zoom_punch` ur kandidatlistan i `sfx.py:hit_events_for()`.
**"Musiken ligger för högt"** -> `config.MUSIC_VOLUME`. Bädden syntetiseras i
`music.py` med rms ~0.297; vid 0.30 och amix-vikten 0.6 hamnade den 0.053 i mixen,
alltså **4.5 dB under** en narrationsrad (0.090) och **högre än personens eget ljud**
i ett SHOW-fönster (0.050). Nya värdet 0.14 ger ~11 dB under berättaren.
**"Musiken känns stressig"** -> `config.MUSIC_STYLE` ska vara `"mysterious"`, inte `"climax"`.
**"Inga pling hörs"** -> `config.SFX_ENABLED` är False, eller inga beats fick `zoom_punch`/`shake`.
**"Plinget låter fel"** -> `sfx.py:hit()` och bygg om: `rm -f assets/sfx/hit.wav && python sfx.py`.
**"För mycket effekter"** -> höj `config.CUT_MIN_SEC`/`CUT_MAX_SEC`, eller strama åt `decide_effect()`.
**"Ljudet drunknar"** -> `config.SFX_VOLUME` upp, `config.MUSIC_VOLUME` ner.
**"Ljudet ligger inte på -14 LUFS"** -> se avsnitt 3b: mät levererad fil med
`python audio_check.py output/<fil>.mp4` och jämför med `[mix] delivered`-raden.
**"Fel typsnitt"** -> `assets/fonts/` + `captions.py` (`Arial Black` aliassas till Archivo Black).
