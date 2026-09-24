# MANDATE.md — TVÅNGSREGLER (portad från sunnyv2youtube)

Detta dokument är **bindande**. En video som bryter mot något av nedan är en
**MISSLYCKAD** leverans, oavsett hur snygg den är på andra sätt.
`verify.py` körs automatiskt efter varje render och **REJECTAR** videor som
bryter mot de mätbara reglerna.

## Verktygen i detta repo
1. **`gfx.py`** — studio-assets: glow-titlar, lower thirds, tweet/social-UI,
   stat-kort, timeline, bar-chart, HUD, light leak, cirkelporträtt.
2. **`cinema.py`** — teal/orange-grade, film grain, vinjett och `beat()` som
   snäpper ljudeffekter till musikens tempo. (Syntetiska SFX bor i `sfx.py`.)
3. **`captions.py`** — `ass_kinetic()` kinetic typography: Arial Black,
   keyword-highlight (accent-röd), rotations-jitter, pop-in 62→100 % på 70 ms.
4. **`verify.py`** — maskinell QA enligt detta dokument (anti-slideshow-gate).

## DE 12 TVÅNGSKRAVEN
| # | Krav | Status i denna pipeline |
|---|------|------------------------|
| 1 | Minst 3 äkta klipp MED ljud per 3 min | ✅ source-gates + original-audio-mix |
| 2 | Visual change var 2–6 s | ✅ beats var 2.2–3.6 s (`config.CUT_*`) |
| 3 | Riktiga display-fonts | ✅ Anton/Archivo Black/Bebas Neue bundleade i `assets/fonts/` |
| 4 | Lower third vid person-intro | ✅ auto: `cues.py` -> `gfx.lower_third()` på person-cue |
| 5 | Social-UI-bevis minst 1 | ✅ auto: `cues.py` -> `gfx.tweet_ui()` på social-cue |
| 6 | Data-viz minst 1 | ✅ auto stat-kort från första siffran i manuset |
| 7 | Kinetic captions | ✅ `captions.ass_kinetic()` + punch-word highlight |
| 8 | 2.5D-rörelse på stills | ✅ `render.render_still_zoom()` Ken Burns |
| 9 | Sound design | ✅ auto: `sfx.build_bed()` i mixen, `config.SFX_ENABLED=True` |
| 10 | Cinematic look | ✅ GRADE+GRAIN+VIGNETTE i `render.cinema_pass()` |
| 11 | Beat-sync | ✅ auto: träffar snäpps till bäddens BPM (±80 ms, bild-sync vinner) |
| 12 | verify_build MÅSTE PASSA | ✅ `verify.verify_build()` körs efter varje render |

✅ = implementerat & auto   🔧 = funktion finns, ej auto-kopplad ännu
Inga 🔧 kvar: samtliga tolv kraven är auto-kopplade och mätta i e2e_test.py.

## RÖD LISTA (momentant NEJ)
- Textspels-video: stills + text utan klipp/ljud/motion → verify REJECTAR
- Statiska full-frame stills >6 s utan parallax/zoom
- Musik som överröstar VO eller klipp-ljud
- Att hoppa över verify-gaten

## Verifiera manuellt
```bash
python verify.py output/<video>.mp4 <target_sekunder>
```
