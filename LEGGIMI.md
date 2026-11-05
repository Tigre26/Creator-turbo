# Creator Turbo – plugin OpenCreator

Versione 0.4.0 (05/11/2026). **Installato sulla Creator 5 Pro il 05/11/2026**: provati in stampa limiti di
temperatura, cambio testina più dolce, attesa piatto, torre riconosciuta dal file, tempi. Le funzioni spente
(carico, tabelle Z, taratura ugello, mesh di riserva) sono controllate solo a secco: 35 prove simulate, macro
generate con lo stesso motore di Kalico.

## Cosa c'è

| File | Contenuto |
| --- | --- |
| `opencreator-plugin.json` | Manifest per `oc-plugin` |
| `klippy/rc_turbo.py` | Modulo Klipper: limite ugelli, guardia camera, cambi testina, tempi, tabella Z, attese |
| `config/creator-turbo.cfg` | **Impostazioni in cima** (sezione `[rc_turbo]`), limiti di temperatura, macro |
| `schermo/grumpyscreen-aggiunte.cfg` | Righe da riportare a mano in `grumpyscreen.cfg` |
| `tests/test_rc_turbo.py` | Prove a secco |

Niente file di OpenCreator modificati: il modulo si aggancia ai loro comandi all'avvio di Klipper
(`_C5_PRINT_START_AFTER_BOOT`, `C5_TOOL_SELECT`, `C5_PRINT_STOP`, `BED_MESH_PROFILE`, `C5_LOAD_FILAMENT`,
più i comandi cronometrati) e alla loro formula dello Z (`_print_correction`). Se un loro aggiornamento
cambia questi nomi, in `klippy.log` compare "rc_turbo: comando … non trovato" e quella parte si spegne da sola.

**Numerazione:** per Riccardo le testine sono **1–4** (pulsanti, messaggi, schermo, Orca). Dentro Klipper, OpenCreator e nei file G-code restano T0–T3 (testina 1 = T0 … testina 4 = T3): cambiarle romperebbe Orca, AFC e OpenCreator.

## Funzioni e interruttori (`[rc_turbo]`)

| Interruttore | Di partenza | Cosa fa |
| --- | --- | --- |
| `limite_ugelli` | acceso | Max 2 ugelli in riscaldamento insieme; il terzo aspetta in coda e parte da solo |
| `guardia_camera` | acceso | Riscaldatore camera al massimo e temperatura in calo lento → abbassa il target (max −10 °C, mai sotto 45) prima del blocco; calo rapido = guasto possibile, decide la protezione |
| `tempi` | acceso | `[tempi]` in console per homing, attesa piatto, mesh, linea, spurgo, cambi, totale; tutto anche in `rc_tempi.csv` |
| `spurgo_prima_presa` | acceso | 1 colore → solo linea di innesco. Più colori con torre (letta dal file di Orca) → la prima testina con la linea, le altre sulla torre, nessuno spurgo nello scivolo. Torre spenta → riserva: 25 mm nello scivolo + silicone alla prima presa, 12 mm alle successive |
| `carico` | acceso (dal 05/11/2026, in prova) | `CARICA_1…CARICA_4`, anche dal "Load" dello schermo |
| `tabella_z_piatto` | spento | Z offset dalla tabella tarata con `RC_TARA_Z` (servono ≥ 2 punti) |
| `tabella_z_ugello` | spento | Z offset dell'ugello per testina dalla tabella di `RC_TARA_UGELLO` invece della stima (T − 120) × 0,00045 |
| `riserva_mesh` | spento | A livellamento spento carica `rc_base` invece di `default` |
| `preriscaldo_partenza` | acceso | Durante homing e riscaldo piatto le testine che OpenCreator porta a 100 °C salgono a 150 °C (mai oltre la temperatura di stampa): partenza più corta, stesse 2 testine |
| `piatto_giu_fine` | acceso | A fine stampa il piatto scende di 50 mm (`piatto_giu_mm`), al massimo fino al limite di OpenCreator (Z 246) se il pezzo è alto. Serve l'interruttore "Lower Bed On End" acceso |

Limiti di temperatura applicati subito: ugelli 320 °C, piatto 125 °C, camera 70 °C.

## Comandi

- `RC_STATO` – interruttori, torre, tabella Z, temperature massime effettive.
- `RC_Z_TESTINA [TESTINA=1..4] [Z=mm | AGGIUNGI=mm]` – correzione Z fissa per testina, fuori stampa (negativo = più schiacciato, limite ±0,3 mm). È lo stesso valore che OpenCreator aggiorna con il baby-step in stampa (`zoffset.json`); senza parametri mostra i valori. Esempio: `RC_Z_TESTINA TESTINA=4 AGGIUNGI=-0.02`.
- `CARICA_1…CARICA_4` [TEMP=] [MATERIAL=] – temperatura = tabella materiale (o materiale della bobina in AFC, o TEMP) + 30 °C.
- `RC_TARA_Z TEMP=60 [TOOL=0] [HOTEND=220]` → baby-step mentre stampa il quadrato → `RC_TARA_Z_QUADRATO` (ne stampa un altro a fianco) → `RC_TARA_Z_SALVA` / `RC_TARA_Z_ANNULLA`. `RC_TABELLA_Z [CANCELLA=60|TUTTO]`.
- `RC_TARA_UGELLO TOOL=n BUILDPLATE_REMOVED=1` (oppure `TOOLS=0,2` o `TOOLS=ALL`, una testina dopo l'altra; si rifà solo per la testina a cui cambi l'ugello, o quando a inizio stampa compare il promemoria dopo 500 ore di stampa di quella testina) – filamento tolto (consigliato), homing, piatto spento e tolto, punta pulita: tocchi sul piattello a freddo → 300 → 260 → 220 °C → freddo (dal più caldo: una sola salita, raffreddamento finale più breve); in mezzo interpolazione. Si scarta da sola se i tocchi non tornano. Valori oltre ±0,15 mm rifiutati (anche per il piatto). `RC_TABELLA_Z CANCELLA_UGELLO=1`.
- `TARA_UGELLO_1…TARA_UGELLO_4`, `TARA_UGELLO_TUTTE` – **procedura guidata a finestre** (Fluidd: Macro; schermo: Console → Macro): avvisa se c'è filamento, fa l'homing con il piatto montato, poi chiede di togliere il piatto, alla fine dice l'esito e ricorda di rimontarlo. La prima volta da Fluidd, per leggere i messaggi e avere l'arresto di emergenza a portata.
- `RC_MESH_BASE BED=60` – mesh completa a piatto stabile salvata come `rc_base` (poi `SAVE_CONFIG`).
- `RC_PROVA_SONDA` – 10 letture al centro: dice se si può passare a 1 tocco per punto.
- `RC_PROVA_PULIZIA [TESTINA=1] [PASSATE=] [DX=]` – a freddo, porta la testina sul silicone.
- `RC_TORRE ATTIVA=1|0|AUTO` – forza la torre per la stampa in corso.

## Installazione (dopo OpenCreator e la prima stampa di prova)

0. **Da GitHub (consigliato, dal 06/11/2026):** il plugin è su https://github.com/Tigre26/Creator-turbo. Una volta sola: `strumenti\installa-plugin-da-github.bat` (sul PC, nella cartella del progetto). Dopo, gli aggiornamenti compaiono in Fluidd tra gli aggiornamenti software come `oc-plugin-creator-turbo`: basta premere Aggiorna.
0. Senza GitHub: doppio clic su `strumenti\installa-plugin.bat` (copia + installazione, chiede solo la password). Per tornare indietro: `strumenti\togli-plugin.bat`.
1. A mano: copiare la cartella `plugin` sulla stampante, per esempio in `/usr/data/creator-turbo` (WinSCP o
   `scp -r plugin opencreator@<IP>:/usr/data/creator-turbo`).
2. Via SSH: `oc-plugin install /usr/data/creator-turbo`. Klipper si riavvia da solo (non durante una stampa).
3. In console Fluidd: `RC_STATO`. Devono comparire le temperature massime 320 / 125 / 70.
4. Dopo ogni modifica dei file: reinstallare con lo stesso comando (l'installazione da cartella è una copia).
   Con un repository GitHub gli aggiornamenti passerebbero dal pannello aggiornamenti di Fluidd.

Togliere: `oc-plugin remove creator-turbo`.

## Ordine di attivazione (una funzione alla volta)

1. **Così com'è** (limite ugelli, guardia camera, tempi, innesco): 2–3 stampe normali, annotare i `[tempi]`.
2. **Mesh**: `RC_PROVA_SONDA`; se il range è sotto 0,01 mm attivare `[probe] samples: 1` e confrontare il tempo della mesh. Poi `RC_MESH_BASE BED=60` + `SAVE_CONFIG` e `riserva_mesh: True`.
3. **Innesco**: stampa a 2 colori con torre: controllare che la seconda testina estruda bene già sulla prima passata della torre. La riserva senza torre si prova dopo `RC_PROVA_PULIZIA`.
4. **Carico**: `carico: True`, poi `CARICA_1` da console; misurare quanti mm servono davvero dal sensore all'ugello e accorciare `carico_veloce_mm`. Poi le righe dello schermo.
5. **Tabella Z dell'ugello**: `RC_TARA_UGELLO` per ogni testina, poi `tabella_z_ugello: True`.
6. **Tabella Z del piatto** (dopo quella dell'ugello): `RC_TARA_Z TEMP=60` e `RC_TARA_Z TEMP=110` (stesso giorno, stessa testina, ugello 220 °C), poi `tabella_z_piatto: True` e prova ASA.

Ogni passo va in `REGISTRO.md` con cosa, perché e risultato.

## Da verificare sulla macchina

- Che `_C5_PRINT_START_AFTER_BOOT`, `C5_TOOL_SELECT` & co. abbiano questi nomi nella versione installata (`RC_STATO` + `klippy.log`).
- Dimensione e posizione del silicone; quota `cooldown_wipe_z` 1,7 di OpenCreator con l'ugello caldo.
- Che il "Load" della pagina bobine passi da `C5_LOAD_FILAMENT TOOL=n` (allora usa già il nostro carico) o da un'altra strada.
- Quanti secondi servono tra rilevamento del filamento e inizio della spinta (`carico_inserimento_s`).
- Comportamento del limite ugelli con le preparazioni di Orca (riscaldamento anticipato della testina successiva).
- Taratura ugello: che il sensore del piattello scatti bene con l'ugello caldo e che i 3 tocchi tornino entro 0,02 mm; quanto vale il freddo rispetto a `extruder.json` (dice se la fabbrica misura a freddo o a ~120 °C).
