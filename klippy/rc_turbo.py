# Creator Turbo: logica di Riccardo per la Creator 5 Pro sopra OpenCreator.
#
# Caricato dalla sezione [rc_turbo] di config/creator-turbo.cfg.
# Non modifica nessun file di OpenCreator: aggancia i loro comandi al
# momento di "klippy:ready" e aggiunge comandi propri (prefisso RC_).
#
# Funzioni (ognuna con il suo interruttore nella sezione [rc_turbo]):
#   limite_ugelli      al massimo N ugelli in riscaldamento insieme
#   guardia_camera     camera: abbassa il target prima del blocco E0077
#   spurgo_prima_presa spurgo + pulizia alla prima presa di ogni testina
#   carico             RC_CARICA / CARICA_Tn e aggancio al "Load" di OpenCreator
#   tabella_z_piatto   Z offset tarato per temperatura del piatto
#   tabella_z_ugello   Z offset tarato per temperatura dell'ugello, per testina
#   riserva_mesh       a livellamento spento carica la mesh base nominata
#   tempi              tempo di ogni fase in console e in rc_tempi.csv
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import collections
import json
import logging
import math
import os
import re
import time

EXTRUDERS = ('extruder', 'extruder1', 'extruder2', 'extruder3')
TABELLA_Z = 'rc_tabella_z.json'
FILE_TEMPI = 'rc_tempi.csv'
FILE_ORE = 'rc_ore_testine.json'
# Orca scrive le impostazioni in fondo al file: basta leggerne la coda.
CODA_FILE = 256 * 1024
# Lo stock corregge al massimo 0,08 mm: oltre 0,15 mm e' un errore di
# taratura, non la realta'.
LIMITE_TABELLA = 0.15


def tool_of(heater_name):
    """'extruder' -> 0, 'extruder2' -> 2, altro -> None."""
    if heater_name not in EXTRUDERS:
        return None
    return EXTRUDERS.index(heater_name)


def heater_of(tool):
    return EXTRUDERS[tool]


def interpola(punti, x):
    """Interpolazione lineare su {temperatura: valore}; costante fuori."""
    if not punti:
        return None
    xs = sorted(punti)
    if x <= xs[0]:
        return punti[xs[0]]
    if x >= xs[-1]:
        return punti[xs[-1]]
    for a, b in zip(xs, xs[1:]):
        if a <= x <= b:
            return punti[a] + (punti[b] - punti[a]) * (x - a) / (b - a)
    return punti[xs[-1]]


def torre_da_testo(testo):
    """1/0 se il G-code dice se la torre di spurgo e' attiva, None se no."""
    trovato = None
    for m in re.finditer(r'^[ \t]*;[ \t]*(enable_prime_tower|wipe_tower)'
                         r'[ \t]*=[ \t]*(\S+)', testo, re.M):
        trovato = m.group(2).strip().lower() in ('1', 'true')
    return trovato


def materiali_da_testo(testo):
    """'; filament_type = ASA;PETG;PLA;PLA' -> ['ASA', 'PETG', 'PLA', 'PLA']."""
    trovati = None
    for m in re.finditer(r'^[ \t]*;[ \t]*filament_type[ \t]*=[ \t]*(.+)$',
                         testo, re.M):
        trovati = [v.strip().strip('"').upper()
                   for v in m.group(1).split(';')]
    return trovati or []


def leggi_materiali(testo):
    """'PLA:220, PETG:240' -> {'PLA': 220.0, 'PETG': 240.0}."""
    tabella = {}
    for voce in testo.split(','):
        voce = voce.strip()
        if not voce:
            continue
        nome, _, valore = voce.partition(':')
        tabella[nome.strip().upper()] = float(valore)
    return tabella


class RCTurbo:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        cfg_file = self.printer.get_start_args().get('config_file', '')
        self.cartella = os.path.dirname(cfg_file) or '.'

        # --- interruttori -------------------------------------------------
        self.limite_ugelli = config.getboolean('limite_ugelli', True)
        self.guardia_camera = config.getboolean('guardia_camera', True)
        self.spurgo_prima_presa = config.getboolean('spurgo_prima_presa',
                                                    True)
        self.carico = config.getboolean('carico', False)
        self.pulizia_cambio = config.getboolean('pulizia_cambio', False)
        self.attesa_materiali = config.getboolean('attesa_materiali', False)
        self.temp_attesa = leggi_materiali(config.get(
            'temperature_attesa', 'PLA:140, PETG:160, ASA:170, ABS:170'))
        # Dopo un cambio con torre: mm in piu' alla prima ripresa (de-retract)
        # per il materiale che nel dock ha colato e lascia l'ugello mezzo vuoto
        self.ripresa_cambio = config.getboolean('ripresa_cambio', False)
        self.ripresa_extra = leggi_materiali(config.get(
            'ripresa_extra', 'PETG:0.5'))
        for k, v in self.ripresa_extra.items():
            if not 0. <= v <= 2.:
                raise config.error('ripresa_extra %s: tra 0 e 2 mm' % k)
        self.ripresa = 0.
        self.riprese_fatte = 0
        self.pulizia_cambio_z_max = config.getfloat('pulizia_cambio_z_max',
                                                    120., minval=0.)
        self.tabella_z_piatto = config.getboolean('tabella_z_piatto', False)
        # Al posto del -0,08 fisso di OpenCreator con piatto >= 100 C
        # (negativo = ugello piu' vicino). Vuoto = come OpenCreator.
        self.piatto_caldo_mm = config.getfloat('piatto_caldo_mm', None,
                                               minval=-0.15, maxval=0.05)
        self.tabella_z_ugello = config.getboolean('tabella_z_ugello', False)
        self.riserva_mesh = config.getboolean('riserva_mesh', False)
        self.tempi = config.getboolean('tempi', True)
        self.preriscaldo_partenza = config.getboolean('preriscaldo_partenza',
                                                      False)
        self.preriscaldo_temp = config.getfloat('preriscaldo_partenza_temp',
                                                150., minval=100.,
                                                maxval=180.)
        self.piatto_giu = config.getboolean('piatto_giu_fine', False)
        self.piatto_giu_mm = config.getfloat('piatto_giu_mm', 50., minval=0.,
                                             maxval=250.)

        # --- limite ugelli ------------------------------------------------
        self.max_ugelli = config.getint('max_ugelli_in_riscaldamento', 2,
                                        minval=1, maxval=4)
        self.banda = config.getfloat('banda_riscaldamento', 5., minval=0.)

        # --- guardia camera -----------------------------------------------
        self.camera_nome = config.get('camera_riscaldatore', 'chamber_heater')
        self.camera_saturo = config.getfloat('camera_saturazione', 0.95,
                                             minval=0.5, maxval=1.)
        self.camera_anticipo = config.getfloat('camera_anticipo', 1.,
                                               minval=0.)
        self.camera_conferma = config.getfloat('camera_conferma_s', 20.,
                                               minval=1.)
        self.camera_calo_max = config.getfloat('camera_calo_max_c_min', 2.,
                                               above=0.)
        self.camera_riduzione_max = config.getfloat('camera_riduzione_max',
                                                    10., minval=0.)
        self.camera_minimo = config.getfloat('camera_minimo', 45.)

        # --- spurgo, pulizia, carico, taratura ---------------------------
        # Letti dalle macro tramite printer.rc_turbo: un solo posto.
        self.valori = {}
        for nome, default in (
                ('spurgo_prima_mm', 25.), ('spurgo_cambio_mm', 12.),
                ('spurgo_velocita', 3.), ('alzata_cambio', 2.),
                ('pulizia_passate', 0), ('pulizia_dx', 5.),
                ('pulizia_velocita', 3000.), ('pulizia_ventola', 0.),
                ('pulizia_raffredda', 0.), ('pulizia_quota_uscita', 10.),
                ('carico_aumento', 30.), ('carico_temp_max', 300.),
                ('carico_veloce_mm', 250.), ('carico_veloce_v', 5.),
                ('carico_lento_mm', 45.), ('carico_lento_v', 3.),
                ('carico_attesa_s', 120.), ('carico_inserimento_s', 3.),
                ('carico_raffredda', 0.),
                ('tara_soak_s', 300.), ('tara_ugello', 220.),
                ('tara_lato_mm', 30.), ('tara_linea_mm', 0.5),
                ('tara_layer_mm', 0.2), ('tara_velocita', 25.),
                ('tara_recupero_mm', 5.), ('tara_filamento_mm', 1.75),
                ('tara_ugello_soak_s', 60.), ('tara_ugello_tocchi', 3),
                ('tara_ugello_spurgo_mm', 10.),
                ('tara_ugello_scarto_tocchi', 0.02),
                ('tara_ugello_scarto_freddo', 0.01),
                ('tara_ugello_margine_freddo', 5.),
                ('mesh_base_soak_s', 300.),
                ('attesa_piatto_caldo_da', 80.),
                ('attesa_piatto_caldo_s', 90.)):
            if isinstance(default, int):
                self.valori[nome] = config.getint(nome, default, minval=0)
            else:
                self.valori[nome] = config.getfloat(nome, default)
        self.mesh_riserva = config.get('mesh_profilo_riserva', 'rc_base')
        self.temp_tara_ugello = [float(x) for x in config.get(
            'tara_ugello_temperature', '220, 260, 300').split(',')
            if x.strip()]
        self.materiali = leggi_materiali(config.get(
            'temperature_materiali', 'DEFAULT:220'))
        if 'DEFAULT' not in self.materiali:
            raise config.error("temperature_materiali: manca DEFAULT")

        # --- stato --------------------------------------------------------
        self.in_partenza = False
        self.mesh_fatta = False
        self.in_cambio = False
        self.pronte = set()          # testine gia' spurgate in questa stampa
        self.temp_testine = {}       # temperature di stampa dal file
        self.torre_file = None       # dal G-code
        self.materiali_file = []     # filament_type dal G-code, per testina
        self.torre_forzata = None    # RC_TORRE ATTIVA=0/1
        self.in_coda = collections.OrderedDict()   # nome -> (heater, temp)
        self.camera = {'target_originale': None, 'raggiunto': False,
                       'ultimo_target': 0., 'sotto_da': None,
                       'storia': collections.deque(maxlen=180),
                       'avvisato_minimo': False}
        self.taratura = None         # dati di RC_TARA_Z in corso
        self.testina_z = None        # testina di cui OpenCreator calcola lo Z
        self.avvisi_ugello = set()
        self.tabella_z, self.tabella_ugello = self._carica_tabella_z()
        self.ore = self._carica_ore()       # ore di stampa per testina
        self.usate_stampa = set()
        self.promemoria_ore = config.getfloat('tara_ugello_promemoria_ore',
                                              500., minval=0.)
        self.fasi = []               # (nome, secondi) della partenza
        self.cambi = []              # secondi di ogni cambio in stampa
        self.ultima_testina = None   # testina montata dall'ultimo cambio
        self.inizio_stampa = None
        self.pheaters = None
        self.orig_set_temperature = None

        self.printer.register_event_handler('klippy:ready', self._pronto)
        self.printer.register_event_handler('klippy:shutdown',
                                            self._spegnimento)
        for nome, funz, desc in (
                ('RC_STATO', self.cmd_stato, 'Stato di Creator Turbo'),
                ('RC_TORRE', self.cmd_torre,
                 'ATTIVA=1/0/AUTO: torre di spurgo per questa stampa'),
                ('RC_ATTENDI_FILAMENTO', self.cmd_attendi_filamento,
                 'T=n [TIMEOUT=s]: aspetta il filamento nel sensore'),
                ('RC_VERIFICA_FILAMENTO', self.cmd_verifica_filamento,
                 'T=n: controlla che il sensore veda il filamento'),
                ('RC_ATTESA', self.cmd_attesa,
                 'SECONDI=s [MOTIVO=...]: attesa senza bloccare l\'MCU'),
                ('RC_TARA_Z', self.cmd_tara_z,
                 'TEMP=piatto [TOOL=0] [HOTEND=220]: taratura Z guidata'),
                ('RC_TARA_Z_SALVA', self.cmd_tara_z_salva,
                 'Salva il baby-step della taratura in corso'),
                ('RC_TARA_Z_ANNULLA', self.cmd_tara_z_annulla,
                 'Annulla la taratura Z in corso'),
                ('RC_TABELLA_Z', self.cmd_tabella_z,
                 '[CANCELLA=temp|TUTTO] [CANCELLA_UGELLO=1..4]: tabelle Z'),
                ('RC_Z_TESTINA', self.cmd_z_testina,
                 '[TESTINA=1..4] [Z=mm | AGGIUNGI=mm]: correzione Z fissa '
                 'per testina (zoffset.json di OpenCreator)'),
                ('RC_TARA_UGELLO', self.cmd_tara_ugello,
                 'TOOL=n o TOOLS=0,2|ALL BUILDPLATE_REMOVED=1: ugello '
                 'sul piattello a caldo')):
            self.gcode.register_command(nome, funz, desc=desc)

    # ------------------------------------------------------------------
    # Avvio: aggancio ai comandi di OpenCreator
    # ------------------------------------------------------------------
    def _pronto(self):
        self.pheaters = self.printer.lookup_object('heaters')
        if self.limite_ugelli:
            self.orig_set_temperature = self.pheaters.set_temperature
            self.pheaters.set_temperature = self._set_temperature
            orig_off = self.pheaters.turn_off_all_heaters

            def spegni_tutto(print_time=0.):
                self.in_coda.clear()
                return orig_off(print_time)
            self.pheaters.turn_off_all_heaters = spegni_tutto
            self.reactor.register_timer(self._tick_coda, self.reactor.NOW)
        if self.guardia_camera:
            self.reactor.register_timer(self._tick_camera, self.reactor.NOW)
        self._aggancia('_C5_PRINT_START_AFTER_BOOT', self._attorno_partenza)
        self._aggancia('C5_TOOL_SELECT', self._attorno_cambio)
        self._aggancia('C5_PRINT_STOP', self._attorno_fine)
        self._aggancia('BED_MESH_PROFILE', self._attorno_profilo_mesh)
        self._aggancia('C5_LOAD_FILAMENT', self._attorno_carico_oc)
        self._aggancia('C5_AUTO_NOZZLE_Z', self._attorno_z_testina)
        self._aggancia('C5_PRINT_Z_PLAN', self._attorno_piano_z)
        self._aggancia('C5_BED_SOAK', self._attorno_attesa_piatto)
        self._aggancia('C5_HOME_FOR_PRINT', self._attorno_homing)
        self._aggancia('C5_LOWER_BED', self._attorno_piatto_giu)
        self._aggancia('BED_MESH_CALIBRATE', self._attorno_mesh)
        if self.ripresa_cambio:
            self._aggancia('G1', self._attorno_g1)
        for nome, etichetta in (('LINE_PURGE', 'linea di innesco'),
                                ('C5_TOOL_PURGE', 'spurgo')):
            self._aggancia(nome, self._solo_tempo(etichetta))
        tc = self.printer.lookup_object('creator5_toolchanger', None)
        if tc is not None and hasattr(tc, '_print_correction'):
            orig = tc._print_correction

            def correzione(hotend, temp_coefficient, bed, first_layer):
                return self._correzione_z(orig, hotend, temp_coefficient,
                                          bed, first_layer)
            tc._print_correction = correzione
        else:
            logging.warning('rc_turbo: creator5_toolchanger senza '
                            '_print_correction, tabella Z non agganciata')

    def _aggancia(self, nome, attorno):
        vecchio = self.gcode.register_command(nome, None)
        if vecchio is None:
            logging.warning('rc_turbo: comando %s non trovato', nome)
            return
        aiuto = getattr(self.gcode, 'gcode_help', {}).get(nome)

        def nuovo(gcmd):
            return attorno(vecchio, gcmd)
        self.gcode.register_command(nome, nuovo, desc=aiuto)

    def _spegnimento(self):
        self.in_coda.clear()

    # ------------------------------------------------------------------
    # Utilita'
    # ------------------------------------------------------------------
    def _ora(self):
        return self.reactor.monotonic()

    def _info(self, msg):
        self.gcode.respond_info('Creator Turbo: ' + msg)

    def _stato_stampa(self):
        stats = self.printer.lookup_object('print_stats', None)
        if stats is None:
            return 'standby'
        return stats.get_status(self._ora()).get('state', 'standby')

    def _interruttore(self, nome):
        obj = self.printer.lookup_object('output_pin ' + nome, None)
        if obj is None:
            return False
        try:
            return bool(obj.get_status(self._ora()).get('value'))
        except Exception:
            return False

    def _attendi_movimenti(self):
        self.printer.lookup_object('toolhead').wait_moves()

    def _durata(self, secondi):
        secondi = int(round(secondi))
        if secondi < 60:
            return '%d s' % secondi
        return '%d:%02d' % (secondi // 60, secondi % 60)

    def _registra_tempo(self, fase, secondi):
        if not self.tempi:
            return
        self._info('[tempi] %s %s' % (fase, self._durata(secondi)))
        if self.in_partenza:
            self.fasi.append((fase, secondi))
        try:
            sd = self.printer.lookup_object('virtual_sdcard', None)
            nome = os.path.basename(sd.file_path() or '') if sd else ''
            percorso = os.path.join(self.cartella, FILE_TEMPI)
            nuovo = not os.path.exists(percorso)
            with open(percorso, 'a') as f:
                if nuovo:
                    f.write('data;file;fase;secondi\n')
                f.write('%s;%s;%s;%.1f\n' % (
                    time.strftime('%Y-%m-%d %H:%M:%S'),
                    nome.replace(';', ','), fase, secondi))
        except Exception:
            logging.exception('rc_turbo: scrittura tempi')

    def _attorno_mesh(self, vecchio, gcmd):
        try:
            self._solo_tempo('mesh')(vecchio, gcmd)
        finally:
            self.mesh_fatta = True

    def _temp_preriscaldo(self, nome, temp):
        # OpenCreator, in partenza e fino alla mesh, porta le testine a
        # min(100 C, stampa). Con il preriscaldo acceso: min(150 C, stampa).
        # Dopo la mesh (testine in attesa, riscaldo finale) non si tocca.
        if (not self.preriscaldo_partenza or not self.in_partenza
                or self.mesh_fatta or nome not in EXTRUDERS):
            return temp
        stampa = self.temp_testine.get(tool_of(nome), 0.)
        if stampa > 100. and abs(temp - min(100., stampa)) < 0.5:
            return min(self.preriscaldo_temp, stampa)
        return temp

    def _attorno_homing(self, vecchio, gcmd):
        # OpenCreator, prima dell'homing di partenza, porta la testina di
        # stampa (e una seconda) a 100 C mentre scalda il piatto. Con il
        # preriscaldo acceso quelle due salgono invece a preriscaldo_temp
        # (mai oltre la temperatura di stampa): stesse 2 testine, quindi
        # stesso carico sull'alimentatore, ma 50 C in piu' gia' fatti
        # mentre si aspetta piatto, attesa e mesh. Le testine sono nel dock:
        # a 150 C PLA/PETG/ASA non colano.
        if self.in_partenza and self.preriscaldo_partenza:
            ora = self._ora()
            for i in range(4):
                nome = 'extruder' if i == 0 else 'extruder%d' % i
                h = self.pheaters.heaters.get(nome)
                if h is None:
                    continue
                target = h.get_temp(ora)[1]
                stampa = self.temp_testine.get(i, 0.)
                nuovo = min(self.preriscaldo_temp, stampa)
                if abs(target - 100.) < 0.5 and nuovo > target:
                    self.pheaters.set_temperature(h, nuovo)
        self._solo_tempo('homing')(vecchio, gcmd)

    def _attorno_piatto_giu(self, vecchio, gcmd):
        # A fine stampa OpenCreator abbassa il piatto fino a Z lower_bed_z
        # (in fondo). Qui solo piatto_giu_mm sotto il pezzo, mai oltre il
        # loro limite (che sta 2 mm sopra il finecorsa): un pezzo alto
        # scende quanto puo'.
        tc = self.printer.lookup_object('creator5_toolchanger', None)
        limite = getattr(tc, 'lower_bed_z', None)
        if not self.piatto_giu or limite is None:
            return vecchio(gcmd)
        toolhead = self.printer.lookup_object('toolhead')
        z = toolhead.get_position()[2]
        tc.lower_bed_z = min(limite, z + self.piatto_giu_mm)
        try:
            vecchio(gcmd)
        finally:
            tc.lower_bed_z = limite

    def _attorno_attesa_piatto(self, vecchio, gcmd):
        # OpenCreator ha una sola durata (bed_soak_minutes, nel cfg 30 s da
        # 50 C). Con il piatto da attesa_piatto_caldo_da in su (ASA/ABS) la
        # allunga a attesa_piatto_caldo_s, solo per questa chiamata.
        tc = self.printer.lookup_object('creator5_toolchanger', None)
        originale = getattr(tc, 'bed_soak_minutes', None)
        cambiata = False
        if originale is not None and 'MINUTES' not in \
                gcmd.get_command_parameters():
            bed = self.pheaters.heaters.get('heater_bed')
            if bed is not None:
                target = bed.get_temp(self._ora())[1]
                if target >= self.valori['attesa_piatto_caldo_da']:
                    tc.bed_soak_minutes = max(
                        originale, self.valori['attesa_piatto_caldo_s'] / 60.)
                    cambiata = True
        try:
            self._solo_tempo('attesa piatto')(vecchio, gcmd)
        finally:
            if cambiata:
                tc.bed_soak_minutes = originale

    def _solo_tempo(self, etichetta):
        def attorno(vecchio, gcmd):
            if not self.tempi:
                return vecchio(gcmd)
            self._attendi_movimenti()
            inizio = self._ora()
            vecchio(gcmd)
            self._attendi_movimenti()
            self._registra_tempo(etichetta, self._ora() - inizio)
        return attorno

    # ------------------------------------------------------------------
    # Limite ugelli in riscaldamento (stock e Reforge ce l'hanno, Kalico no)
    # ------------------------------------------------------------------
    def _in_riscaldamento(self, escluso=None):
        ora = self._ora()
        n = 0
        for nome in EXTRUDERS:
            h = self.pheaters.heaters.get(nome)
            if h is None or h is escluso:
                continue
            temp, target = h.get_temp(ora)
            if target > 0. and temp < target - self.banda:
                n += 1
        return n

    def _set_temperature(self, heater, temp, wait=False):
        nome = heater.get_name()
        if nome not in EXTRUDERS:
            return self.orig_set_temperature(heater, temp, wait)
        temp = self._temp_preriscaldo(nome, temp)
        temp = self._temp_attesa(heater, temp)
        # Un nuovo comando sostituisce quello in coda per lo stesso ugello
        self.in_coda.pop(nome, None)
        attuale, _ = heater.get_temp(self._ora())
        if (temp <= 0. or temp <= attuale + self.banda
                or self._in_riscaldamento(escluso=heater) < self.max_ugelli):
            return self.orig_set_temperature(heater, temp, wait)
        if temp < heater.min_temp or temp > heater.max_temp:
            # Lascia dare l'errore di fuori range a Klipper
            return self.orig_set_temperature(heater, temp, wait)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(lambda pt: None)
        self.in_coda[nome] = (heater, temp)
        self._info('testina %d in coda a %.0f C: %d ugelli gia\' in riscaldamento'
                   % (tool_of(nome) + 1, temp, self.max_ugelli))
        if wait:
            ora = self._ora()
            while nome in self.in_coda:
                if self.printer.is_shutdown():
                    return
                ora = self.reactor.pause(ora + .5)
            if heater.target_temp == temp:
                self.orig_set_temperature(heater, temp, True)

    def _tick_coda(self, eventtime):
        if self.in_coda and not self.printer.is_shutdown():
            for nome, (heater, temp) in list(self.in_coda.items()):
                if self._in_riscaldamento(escluso=heater) >= self.max_ugelli:
                    break
                del self.in_coda[nome]
                heater.set_temp(temp)
                self._info('testina %d parte a %.0f C' % (tool_of(nome) + 1, temp))
        return eventtime + .5

    # ------------------------------------------------------------------
    # Guardia camera (caso E0077: ASA, camera 65 -> 57 C, stanza fredda)
    # ------------------------------------------------------------------
    def _isteresi_camera(self):
        cf = self.printer.lookup_object('configfile')
        impostazioni = cf.get_status(self._ora()).get('settings', {})
        sez = impostazioni.get('verify_heater ' + self.camera_nome, {})
        return float(sez.get('hysteresis', 5.))

    def _tick_camera(self, eventtime):
        try:
            self._controlla_camera(eventtime)
        except Exception:
            logging.exception('rc_turbo: guardia camera')
        return eventtime + 1.

    def _controlla_camera(self, ora):
        h = self.pheaters.heaters.get(self.camera_nome) if self.pheaters \
            else None
        if h is None or self.printer.is_shutdown():
            return
        c = self.camera
        temp, target = h.get_temp(ora)
        c['storia'].append((ora, temp))
        if target != c['ultimo_target']:
            # Target cambiato da altri (slicer, schermo): si riparte da capo
            c.update(target_originale=None, raggiunto=False, sotto_da=None,
                     avvisato_minimo=False)
            c['ultimo_target'] = target
        if target <= 0.:
            return
        isteresi = self._isteresi_camera()
        if temp >= target - isteresi:
            c['raggiunto'] = True
        potenza = getattr(h, 'last_pwm_value', 0.)
        saturo = potenza >= self.camera_saturo * h.get_max_power()
        # Interviene solo dopo aver raggiunto il target: in salita il
        # riscaldatore e' al massimo per natura.
        if not (c['raggiunto'] and saturo
                and temp < target - isteresi + self.camera_anticipo):
            c['sotto_da'] = None
            return
        if c['sotto_da'] is None:
            c['sotto_da'] = ora
        if ora - c['sotto_da'] < self.camera_conferma:
            return
        # Calo lento = stanza fredda; calo rapido = possibile guasto
        # (sonda staccata): in quel caso decide verify_heater.
        vecchi = [t for (s, t) in c['storia'] if ora - s >= 60.]
        if not vecchi or vecchi[-1] - temp > self.camera_calo_max:
            return
        originale = c['target_originale'] or target
        nuovo = math.floor(temp + isteresi - 2.)
        nuovo = max(nuovo, originale - self.camera_riduzione_max)
        if nuovo < self.camera_minimo or nuovo >= target:
            if not c['avvisato_minimo']:
                c['avvisato_minimo'] = True
                self._info('camera a %.1f C con riscaldatore al massimo: '
                           'limite di riduzione raggiunto, decide la '
                           'protezione. Chiudi gli sportelli.' % temp)
            return
        h.set_temp(nuovo)
        c['target_originale'] = originale
        c['ultimo_target'] = nuovo
        c['sotto_da'] = None
        self._info('camera a %.1f C con riscaldatore al massimo: target '
                   '%.0f -> %.0f C per evitare il blocco (originale %.0f C).'
                   ' Chiudi gli sportelli.' % (temp, target, nuovo,
                                               originale))

    # ------------------------------------------------------------------
    # Partenza stampa, cambi testina, fine stampa
    # ------------------------------------------------------------------
    def _parametri(self, gcmd):
        out = {}
        for k, v in gcmd.get_command_parameters().items():
            out[k.upper()] = str(v).strip().strip('"\'')
        return out

    def _elenco(self, testo):
        return sorted({int(x) for x in testo.split(',')
                       if x.strip() in ('0', '1', '2', '3')})

    def _coda_file(self):
        sd = self.printer.lookup_object('virtual_sdcard', None)
        percorso = sd.file_path() if sd is not None else None
        if not percorso:
            return ''
        try:
            with open(percorso, 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - CODA_FILE))
                return f.read().decode('utf-8', 'replace')
        except OSError:
            return ''

    def _torre_dal_file(self):
        testo = self._coda_file()
        self.materiali_file = materiali_da_testo(testo)
        return torre_da_testo(testo) if testo else None

    def _temp_attesa(self, heater, temp):
        # In stampa Orca mette le testine posate a (stampa - 80 C): il PETG
        # a 175 e l'ASA a 190 colano nel dock. Qui al massimo la temperatura
        # di attesa del materiale. Solo per abbassare, solo testine non
        # montate, mai durante la partenza (ci pensa OpenCreator).
        if (not self.attesa_materiali or self.in_partenza or temp <= 0.
                or self._stato_stampa() != 'printing'):
            return temp
        # Orca manda l'attesa ("cooldown") poco PRIMA del cambio, quando la
        # testina e' ancora montata: per questo non si guarda quale e'
        # montata, ma serve una stampa a piu' testine e un valore di attesa
        # (almeno 30 C sotto la temperatura di stampa).
        if len(self.usate_stampa) < 2:
            return temp
        tool = tool_of(heater.get_name())
        if tool >= len(self.materiali_file):
            return temp
        materiale = self.materiali_file[tool]
        stampa = (self.temp_testine.get(tool, 0.)
                  or self.materiali.get(materiale, 0.))
        if not stampa or temp > stampa - 30.:
            return temp
        limite = self.temp_attesa.get(materiale)
        if limite is None or temp <= limite:
            return temp
        return limite

    def _torre(self):
        if self.torre_forzata is not None:
            return self.torre_forzata
        return bool(self.torre_file)

    def _attorno_partenza(self, vecchio, gcmd):
        p = self._parametri(gcmd)
        tool = int(p.get('TOOL', '0') or 0)
        hotend = float(p.get('HOTEND', '0') or 0)
        self.temp_testine = {}
        self.ripresa = 0.
        self.riprese_fatte = 0
        self.mesh_fatta = False
        for i in range(4):
            try:
                self.temp_testine[i] = float(p.get('HOTEND%d' % i, hotend))
            except ValueError:
                self.temp_testine[i] = hotend
        scelte = self._elenco(p.get('TOOLS', str(tool)))
        usate = self._elenco(p.get('USED_TOOLS', ''))
        self.torre_file = self._torre_dal_file()
        self.torre_forzata = None
        self.usate_stampa = set(usate) | set(scelte) | {tool}
        self._promemoria_taratura(self.usate_stampa)
        self.pronte = set()
        self.ultima_testina = None
        self.fasi = []
        self.cambi = []
        self._attendi_movimenti()
        self.inizio_stampa = inizio = self._ora()
        self.in_partenza = True
        if self.spurgo_prima_presa and len(set(usate) | {tool}) > 1:
            self._info('stampa a %d colori, torre di spurgo %s%s' % (
                len(set(usate) | {tool}),
                {True: 'attiva', False: 'spenta', None: 'non indicata'}[
                    self.torre_file],
                '' if self.torre_file else
                ': le altre testine spurgano nello scivolo alla prima presa'))
        try:
            vecchio(gcmd)
            self._attendi_movimenti()
        finally:
            self.in_partenza = False
        self.pronte = {tool}
        self.ultima_testina = tool
        if self._interruttore('purge') or self._interruttore(
                'flow_calibration'):
            self.pronte |= set(scelte)
        if (self._interruttore('purge_line')
                and self._interruttore('purge_line_all_tools')):
            self.pronte |= set(scelte) | set(usate)
        if self.tempi and self._stato_stampa() == 'printing':
            totale = self._ora() - inizio
            dettaglio = ', '.join('%s %s' % (f, self._durata(s))
                                  for f, s in self.fasi)
            self._registra_tempo('preparazione totale', totale)
            if dettaglio:
                self._info('[tempi] dettaglio: ' + dettaglio)

    def _temp_stampa(self, tool):
        temp = self.temp_testine.get(tool, 0.)
        if temp > 0.:
            return temp
        h = self.pheaters.heaters.get(heater_of(tool)) if self.pheaters \
            else None
        if h is not None and h.target_temp >= 170.:
            return h.target_temp
        return max(self.temp_testine.values() or [220.]) or 220.

    def _attorno_cambio(self, vecchio, gcmd):
        tool = gcmd.get_int('T', None)
        in_stampa = (self._stato_stampa() == 'printing'
                     and not self.in_partenza and not self.in_cambio)
        if in_stampa and self.tempi:
            self._attendi_movimenti()
        inizio = self._ora()
        # OpenCreator richiama anche la testina gia' montata (inizio stampa,
        # primo layer): non e' un cambio, niente tempo ne' spurgo.
        stessa = tool is not None and tool == self.ultima_testina
        vecchio(gcmd)
        if tool is not None:
            self.ultima_testina = tool
        self.ripresa = 0.
        if not in_stampa or tool is None or stessa:
            return
        if self.ripresa_cambio and self._torre():
            self.ripresa = self._extra_ripresa(tool)
        if self.tempi:
            self._attendi_movimenti()
            durata = self._ora() - inizio
            self.cambi.append(durata)
            self._registra_tempo('cambio testina %d' % (tool + 1), durata)
        if not self.spurgo_prima_presa:
            return
        # Con la torre attiva la innesca la torre (anche alla prima presa);
        # senza torre si spurga nello scivolo come riserva.
        if self._torre():
            lunghezza, motivo = 0., 'torre attiva'
            self._pulisci_dopo_cambio(tool)
        elif tool not in self.pronte:
            lunghezza, motivo = self.valori['spurgo_prima_mm'], \
                'prima presa senza torre'
        else:
            lunghezza, motivo = self.valori['spurgo_cambio_mm'], 'senza torre'
        self.pronte.add(tool)
        if lunghezza <= 0.:
            return
        self._info('testina %d %s: spurgo %.0f mm + pulizia' % (tool + 1, motivo,
                                                        lunghezza))
        self.in_cambio = True
        inizio = self._ora()
        try:
            self.gcode.run_script_from_command(
                '_RC_SPURGO_CAMBIO T=%d LENGTH=%.1f TEMP=%.1f'
                % (tool, lunghezza, self._temp_stampa(tool)))
            self._attendi_movimenti()
        finally:
            self.in_cambio = False
        if self.tempi:
            self._registra_tempo('spurgo testina %d (%s)' % (tool + 1, motivo),
                                 self._ora() - inizio)

    def _extra_ripresa(self, tool):
        if tool >= len(self.materiali_file):
            return 0.
        return self.ripresa_extra.get(self.materiali_file[tool], 0.)

    def _attorno_g1(self, vecchio, gcmd):
        # Solo il primo movimento con E dopo un cambio: se e' la ripresa di
        # Orca (solo E, positiva, estrusione relativa) aggiunge i mm in piu'.
        # Qualunque altro movimento con E annulla senza toccare niente.
        if self.ripresa:
            p = gcmd.get_command_parameters()
            if 'E' in p:
                try:
                    e = float(p['E'])
                except ValueError:
                    e = 0.
                solo_e = not any(k in p for k in ('X', 'Y', 'Z'))
                if solo_e and e < 0.:
                    # ritrazione prima della ripresa: si aspetta la ripresa
                    return vecchio(gcmd)
                extra, self.ripresa = self.ripresa, 0.
                gm = self.printer.lookup_object('gcode_move', None)
                if (e > 0. and solo_e
                        and gm is not None
                        and getattr(gm, 'absolute_extrude', True) is False):
                    p['E'] = '%.5f' % (e + extra)
                    self.riprese_fatte += 1
        return vecchio(gcmd)

    def _pulisci_dopo_cambio(self, tool):
        # La testina posata nel dock, mentre Orca la preriscalda, fa una goccia
        # sull'ugello: senza pulizia cade sul piatto o sul pezzo andando alla
        # torre. Con pulizia_cambio: passata sul silicone prima della torre.
        if not self.pulizia_cambio:
            return
        z = self.printer.lookup_object('gcode_move').get_status(
            self._ora())['gcode_position'][2]
        if z > self.pulizia_cambio_z_max:
            return
        self.in_cambio = True
        inizio = self._ora()
        try:
            self.gcode.run_script_from_command('_RC_PULIZIA_CAMBIO')
            self._attendi_movimenti()
        finally:
            self.in_cambio = False
        if self.tempi:
            self._registra_tempo('pulizia testina %d' % (tool + 1),
                                 self._ora() - inizio)

    def _attorno_fine(self, vecchio, gcmd):
        self.ripresa = 0.
        if self.inizio_stampa is not None and self.usate_stampa:
            stats = self.printer.lookup_object('print_stats', None)
            durata = 0.
            if stats is not None:
                durata = stats.get_status(self._ora()).get(
                    'print_duration', 0.) or 0.
            self._aggiungi_ore(self.usate_stampa, durata)
            self.usate_stampa = set()
        vecchio(gcmd)
        if self.tempi and self.inizio_stampa is not None:
            if self.cambi:
                self._info('[tempi] %d cambi testina, media %s, totale %s'
                           % (len(self.cambi),
                              self._durata(sum(self.cambi) / len(self.cambi)),
                              self._durata(sum(self.cambi))))
            self._registra_tempo('stampa completa',
                                 self._ora() - self.inizio_stampa)
        self.inizio_stampa = None
        self.pronte = set()
        self.torre_forzata = None

    def _attorno_profilo_mesh(self, vecchio, gcmd):
        if (self.riserva_mesh and self.in_partenza
                and gcmd.get('LOAD', '') == 'default'):
            bm = self.printer.lookup_object('bed_mesh', None)
            profili = bm.get_status(self._ora()).get('profiles', {}) \
                if bm is not None else {}
            if self.mesh_riserva in profili:
                self._info('livellamento spento: carico la mesh base "%s"'
                           % self.mesh_riserva)
                self.gcode.run_script_from_command(
                    'BED_MESH_PROFILE LOAD=%s' % self.mesh_riserva)
                return
            self._info('mesh base "%s" assente: carico "default"'
                       % self.mesh_riserva)
        return vecchio(gcmd)

    def _attorno_carico_oc(self, vecchio, gcmd):
        # "Load" della pagina bobine (AFC -> CHANGE_TOOL -> C5_LOAD_FILAMENT
        # TOOL=n): fuori stampa e per una sola testina passa dal nostro carico.
        p = self._parametri(gcmd)
        tool = p.get('TOOL', '')
        if (not self.carico or 'TOOLS' in p or tool not in ('0', '1', '2', '3')
                or self._stato_stampa() in ('printing', 'paused')):
            return vecchio(gcmd)
        cmd = 'RC_CARICA TOOL=%s' % tool
        if 'TEMP' in p:
            cmd += ' TEMP=%s' % p['TEMP']
        self.gcode.run_script_from_command(cmd)

    def cmd_torre(self, gcmd):
        valore = gcmd.get('ATTIVA', '').strip().upper()
        if valore in ('1', '0'):
            self.torre_forzata = valore == '1'
        elif valore == 'AUTO':
            self.torre_forzata = None
        elif valore:
            raise gcmd.error('ATTIVA deve essere 1, 0 o AUTO')
        gcmd.respond_info('Torre di spurgo: %s (file: %s, forzata: %s)' % (
            'attiva' if self._torre() else 'spenta', self.torre_file,
            self.torre_forzata))

    # ------------------------------------------------------------------
    # Filamento
    # ------------------------------------------------------------------
    def _sensore(self, tool):
        for nome in ('filament_switch_sensor extruder%d_tool_start' % tool,
                     'filament_switch_sensor %s_tool_start'
                     % heater_of(tool)):
            obj = self.printer.lookup_object(nome, None)
            if obj is not None:
                return obj
        return None

    def _filamento(self, tool):
        sensore = self._sensore(tool)
        if sensore is None:
            raise self.printer.command_error(
                'Sensore filamento della testina %d non trovato' % (tool + 1))
        return bool(sensore.get_status(self._ora()).get('filament_detected'))

    def cmd_attendi_filamento(self, gcmd):
        tool = gcmd.get_int('T', minval=0, maxval=3)
        attesa = gcmd.get_float('TIMEOUT', self.valori['carico_attesa_s'],
                                minval=1.)
        if self._filamento(tool):
            return
        gcmd.respond_info('Inserisci il filamento nella testina %d (attendo %d s)'
                          % (tool + 1, attesa))
        ora = self._ora()
        fine = ora + attesa
        while not self._filamento(tool):
            if self.printer.is_shutdown():
                return
            if ora >= fine:
                self.gcode.run_script_from_command(
                    '_RC_CARICA_ANNULLA T=%d' % tool)
                raise gcmd.error('testina %d: nessun filamento entro %d s, carico '
                                 'annullato' % (tool + 1, attesa))
            ora = self.reactor.pause(ora + .5)
        # Lascia il tempo di spingere il filamento fino agli ingranaggi
        ritardo = self.valori['carico_inserimento_s']
        gcmd.respond_info('testina %d: filamento rilevato, parto tra %.0f s'
                          % (tool + 1, ritardo))
        self.reactor.pause(self._ora() + ritardo)

    def cmd_verifica_filamento(self, gcmd):
        tool = gcmd.get_int('T', minval=0, maxval=3)
        if self._filamento(tool):
            gcmd.respond_info('testina %d caricato: il sensore vede il filamento'
                              % (tool + 1))
        else:
            raise gcmd.error('testina %d: dopo il carico il sensore non vede il '
                             'filamento' % (tool + 1))

    def cmd_attesa(self, gcmd):
        secondi = gcmd.get_float('SECONDI', minval=0., maxval=3600.)
        motivo = gcmd.get('MOTIVO', 'attesa')
        if secondi <= 0.:
            return
        self._attendi_movimenti()
        ora = self._ora()
        fine = ora + secondi
        prossimo = ora
        while ora < fine:
            if self.printer.is_shutdown():
                return
            if ora >= prossimo:
                gcmd.respond_info('%s: mancano %s' % (
                    motivo, self._durata(fine - ora)))
                prossimo = ora + 30.
            ora = self.reactor.pause(min(fine, ora + 1.))

    # ------------------------------------------------------------------
    # Z offset per temperatura del piatto
    # ------------------------------------------------------------------
    def _carica_tabella_z(self):
        try:
            with open(os.path.join(self.cartella, TABELLA_Z)) as f:
                dati = json.load(f)
        except (OSError, ValueError):
            return {}, {}
        try:
            piatto = {float(k): float(v)
                      for k, v in dati.get('piatto', {}).items()}
        except (ValueError, TypeError, AttributeError):
            piatto = {}
        ugello = {}
        for k, v in (dati.get('ugello') or {}).items():
            try:
                ugello[int(k)] = {
                    'riferimento': float(v['riferimento']),
                    'punti': {float(t): float(z)
                              for t, z in v['punti'].items()},
                    'data': v.get('data', ''),
                    'ore': float(v.get('ore', 0.))}
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        return piatto, ugello

    def _carica_ore(self):
        try:
            with open(os.path.join(self.cartella, FILE_ORE)) as f:
                return {int(k): float(v) for k, v in json.load(f).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _aggiungi_ore(self, testine, secondi):
        for t in testine:
            self.ore[t] = self.ore.get(t, 0.) + secondi / 3600.
        try:
            percorso = os.path.join(self.cartella, FILE_ORE)
            with open(percorso + '.tmp', 'w') as f:
                json.dump({str(k): round(v, 2)
                           for k, v in sorted(self.ore.items())}, f)
            os.replace(percorso + '.tmp', percorso)
        except OSError:
            logging.exception('rc_turbo: ore di stampa')

    def _promemoria_taratura(self, testine):
        if not self.promemoria_ore:
            return
        for t in sorted(testine):
            dati = self.tabella_ugello.get(t)
            if not dati:
                continue
            passate = self.ore.get(t, 0.) - dati.get('ore', 0.)
            if passate >= self.promemoria_ore:
                self._info('testina %d: %.0f ore di stampa dalla taratura ugello '
                           '(%s): conviene rifarla (TARA_UGELLO_%d)'
                           % (t + 1, passate, dati['data'], t + 1))

    def _salva_tabella_z(self):
        percorso = os.path.join(self.cartella, TABELLA_Z)
        dati = {'piatto': {('%g' % k): round(v, 4)
                           for k, v in sorted(self.tabella_z.items())},
                'ugello': {str(t): {
                    'riferimento': round(d['riferimento'], 4),
                    'punti': {('%g' % k): round(v, 4)
                              for k, v in sorted(d['punti'].items())},
                    'data': d['data'], 'ore': d.get('ore', 0.)}
                    for t, d in sorted(self.tabella_ugello.items())},
                'aggiornata': time.strftime('%Y-%m-%d %H:%M')}
        temporaneo = percorso + '.tmp'
        with open(temporaneo, 'w') as f:
            json.dump(dati, f, indent=2)
            f.write('\n')
        os.replace(temporaneo, percorso)

    def _tabella_valida(self):
        return self.tabella_z_piatto and len(self.tabella_z) >= 2

    def _punti_ugello(self, tool):
        """Tabella dell'ugello di TOOL se attiva e ancora valida."""
        if not self.tabella_z_ugello or tool is None:
            return None
        dati = self.tabella_ugello.get(tool)
        if not dati:
            return None
        tc = self.printer.lookup_object('creator5_toolchanger', None)
        attuale = tc.measurements[tool][2] if tc is not None else None
        if attuale is None or abs(attuale - dati['riferimento']) > 0.005:
            # Calibrazione offset rifatta dopo la taratura: tabella vecchia
            if tool not in self.avvisi_ugello:
                self.avvisi_ugello.add(tool)
                self._info('tabella ugello testina %d non valida (calibrazione '
                           'offset cambiata): uso la formula. Rifai '
                           'TARA_UGELLO_%d' % (tool + 1, tool + 1))
            return None
        return dati['punti']

    def _correzione_z(self, orig, hotend, coeff, bed, first_layer):
        ugello = self._punti_ugello(self.testina_z)
        extra = 0.
        if ugello:
            # coeff=0 toglie la stima (T - 120) x 0,00045 di OpenCreator
            coeff = 0.
            extra += interpola(ugello, hotend)
        if self.taratura is not None:
            # In taratura nessun termine del piatto: il baby-step lo misura
            return orig(hotend, coeff, 0., first_layer) + extra
        if self._tabella_valida():
            # bed=0 toglie il termine fisso di OpenCreator (-0,08 da 100 C)
            return orig(hotend, coeff, 0., first_layer) + extra + interpola(
                self.tabella_z, bed)
        if self.piatto_caldo_mm is not None:
            # bed=0 toglie il -0,08 di OpenCreator, al suo posto il nostro
            return orig(hotend, coeff, 0., first_layer) + extra + (
                self.piatto_caldo_mm if bed >= 100. else 0.)
        return orig(hotend, coeff, bed, first_layer) + extra

    def _attorno_z_testina(self, vecchio, gcmd):
        self.testina_z = gcmd.get_int('T', None)
        try:
            return vecchio(gcmd)
        finally:
            self.testina_z = None

    def _attorno_piano_z(self, vecchio, gcmd):
        # OpenCreator calcola le 4 testine in un colpo, senza dire quale:
        # con la tabella ugello attiva lo si chiama una testina alla volta.
        if not self.tabella_z_ugello or not self.tabella_ugello:
            return vecchio(gcmd)
        tc = self.printer.lookup_object('creator5_toolchanger')
        p = self._parametri(gcmd)
        comuni = {k: v for k, v in p.items() if not k.startswith('HOTEND')}
        risultato = [None] * 4
        for tool in range(4):
            chiave = 'HOTEND%d' % tool
            if chiave not in p:
                continue
            parametri = dict(comuni)
            parametri[chiave] = p[chiave]
            riga = 'C5_PRINT_Z_PLAN ' + ' '.join(
                '%s=%s' % kv for kv in sorted(parametri.items()))
            sub = self.gcode.create_gcode_command('C5_PRINT_Z_PLAN', riga,
                                                  parametri)
            self.testina_z = tool
            try:
                vecchio(sub)
            finally:
                self.testina_z = None
            risultato[tool] = tc.print_z_corrections[tool]
        tc.print_z_corrections = risultato

    def _toolchanger(self, gcmd):
        tc = self.printer.lookup_object('creator5_toolchanger', None)
        if tc is None or not hasattr(tc, 'z_adjustments'):
            raise gcmd.error('creator5_toolchanger non disponibile')
        return tc

    def cmd_tara_z(self, gcmd):
        if self._stato_stampa() in ('printing', 'paused'):
            raise gcmd.error('Non durante una stampa')
        if self.taratura is not None:
            raise gcmd.error('Taratura gia\' in corso: RC_TARA_Z_SALVA o '
                             'RC_TARA_Z_ANNULLA')
        temp = gcmd.get_float('TEMP', minval=0., maxval=130.)
        tool = gcmd.get_int('TOOL', 0, minval=0, maxval=3)
        hotend = gcmd.get_float('HOTEND', self.valori['tara_ugello'],
                                minval=170., maxval=300.)
        soak = gcmd.get_float('SOAK', self.valori['tara_soak_s'], minval=0.)
        tc = self._toolchanger(gcmd)
        tc._load_zoffset_json()
        self.taratura = {'temp': temp, 'tool': tool, 'hotend': hotend,
                         'adj_iniziale': tc.z_adjustments[tool]}
        try:
            self.gcode.run_script_from_command(
                '_RC_TARA_Z_ESEGUI TEMP=%.1f TOOL=%d HOTEND=%.1f SOAK=%.0f'
                % (temp, tool, hotend, soak))
        except Exception:
            self._ripristina_taratura(gcmd)
            raise

    def _ripristina_taratura(self, gcmd):
        dati, self.taratura = self.taratura, None
        if dati is None:
            return
        tc = self._toolchanger(gcmd)
        tc._load_zoffset_json()
        tool = dati['tool']
        if tc.z_adjustments[tool] != dati['adj_iniziale']:
            # Il baby-step della prova non deve restare come regolazione
            # della testina: OpenCreator lo aveva salvato in zoffset.json
            tc._write_z_adjustment(gcmd, tool, dati['adj_iniziale'])

    def cmd_tara_z_salva(self, gcmd):
        dati = self.taratura
        if dati is None:
            raise gcmd.error('Nessuna taratura in corso (RC_TARA_Z TEMP=...)')
        tc = self._toolchanger(gcmd)
        tool = dati['tool']
        base = tc.print_z_baseline[tool]
        if base is None:
            raise gcmd.error('Z della testina non applicato: rifai RC_TARA_Z')
        origine = self.printer.lookup_object('gcode_move').get_status(
            )['homing_origin'][2]
        valore = origine - (base + dati['adj_iniziale'])
        if not math.isfinite(valore) or abs(valore) > LIMITE_TABELLA:
            raise gcmd.error('Valore %+.3f mm anomalo (oltre %.2f mm): non '
                             'salvato, rifai la prova' % (valore,
                                                          LIMITE_TABELLA))
        self.tabella_z[round(dati['temp'])] = valore
        self._salva_tabella_z()
        self._ripristina_taratura(gcmd)
        gcmd.respond_info('Tabella Z: piatto %.0f C -> %+.3f mm salvato. '
                          'Punti: %s' % (dati['temp'], valore,
                                         self._testo_tabella()))
        self.gcode.run_script_from_command('_RC_TARA_Z_FINE')

    def cmd_tara_z_annulla(self, gcmd):
        attiva = self.taratura is not None
        self._ripristina_taratura(gcmd)
        if attiva:
            self.gcode.run_script_from_command('_RC_TARA_Z_FINE')
        gcmd.respond_info('Taratura Z annullata')

    def _testo_tabella(self):
        if not self.tabella_z:
            return 'vuota'
        return ', '.join('%g C %+.3f' % (k, v)
                         for k, v in sorted(self.tabella_z.items()))

    def _testo_ugello(self):
        if not self.tabella_ugello:
            return 'vuota'
        return '; '.join('testina %d %s (%s, %.0f ore fa)' % (t + 1, ', '.join(
            '%g C %+.3f' % kv for kv in sorted(d['punti'].items())),
            d['data'], self.ore.get(t, 0.) - d.get('ore', 0.))
            for t, d in sorted(self.tabella_ugello.items()))

    def cmd_tabella_z(self, gcmd):
        ugello = gcmd.get('CANCELLA_UGELLO', '').strip().upper().lstrip('T')
        if ugello:
            try:
                self.tabella_ugello.pop(int(ugello) - 1)
            except (ValueError, KeyError):
                raise gcmd.error('Nessuna tabella ugello per la testina %s' % ugello)
            self._salva_tabella_z()
        cancella = gcmd.get('CANCELLA', '').strip().upper()
        if cancella == 'TUTTO':
            self.tabella_z = {}
            self._salva_tabella_z()
        elif cancella:
            try:
                self.tabella_z.pop(float(cancella))
            except (ValueError, KeyError):
                raise gcmd.error('Nessun punto a %s C' % cancella)
            self._salva_tabella_z()
        stato = ('in uso' if self._tabella_valida() else
                 'non in uso (servono almeno 2 punti e tabella_z_piatto: '
                 'True): vale la formula di OpenCreator')
        gcmd.respond_info('Tabella Z piatto: %s. %s\nTabella Z ugello: %s. %s'
                          % (self._testo_tabella(), stato,
                             self._testo_ugello(),
                             'in uso' if self.tabella_z_ugello else
                             'non in uso (tabella_z_ugello: False)'))

    # ------------------------------------------------------------------
    # Correzione Z fissa per testina, fuori stampa. E' lo stesso valore
    # che OpenCreator aggiorna col baby-step (zoffset.json): qui si scrive
    # a mano, con la numerazione 1-4, senza prendere la testina.
    # ------------------------------------------------------------------
    LIMITE_Z_TESTINA = 0.3

    def cmd_z_testina(self, gcmd):
        tc = self.printer.lookup_object('creator5_toolchanger', None)
        if tc is None or not hasattr(tc, '_write_z_adjustment'):
            raise gcmd.error('rc_turbo: correzione Z di OpenCreator non '
                             'trovata in questa versione')
        tc._load_zoffset_json()
        testina = gcmd.get_int('TESTINA', 0, minval=0, maxval=4)
        z = gcmd.get_float('Z', None)
        aggiungi = gcmd.get_float('AGGIUNGI', None)
        if testina and (z is not None or aggiungi is not None):
            if z is not None and aggiungi is not None:
                raise gcmd.error('Usa Z= oppure AGGIUNGI=, non tutti e due')
            stats = self.printer.lookup_object('print_stats', None)
            stato = stats.get_status(self.reactor.monotonic())['state'] \
                if stats else ''
            if stato in ('printing', 'paused') or getattr(tc, 'busy', False):
                raise gcmd.error('Durante la stampa usa il baby-step: '
                                 'OpenCreator lo salva da solo')
            t = testina - 1
            vecchio = tc.z_adjustments[t]
            nuovo = z if z is not None else vecchio + aggiungi
            if abs(nuovo) > self.LIMITE_Z_TESTINA:
                raise gcmd.error('Correzione %.3f mm oltre +/-%.2f mm: '
                                 'meglio rifare C5_CALIBRATE_OFFSETS'
                                 % (nuovo, self.LIMITE_Z_TESTINA))
            tc._write_z_adjustment(gcmd, t, nuovo)
            gcmd.respond_info('Testina %d: correzione Z %+.3f -> %+.3f mm '
                              '(negativo = ugello piu\' vicino al piatto). '
                              'Vale dalla prossima presa della testina.'
                              % (testina, vecchio, nuovo))
        righe = ['Testina %d: %+.3f mm' % (t + 1, tc.z_adjustments[t])
                 for t in range(4) if not testina or t == testina - 1]
        gcmd.respond_info('Correzione Z per testina (baby-step salvato):\n'
                          + '\n'.join(righe))

    # ------------------------------------------------------------------
    # Taratura Z dell'ugello sul piattello di calibrazione
    # freddo -> 300 -> 260 -> 220 -> freddo, 3 tocchi per temperatura.
    # Il tocco e' quello di OpenCreator (_probe_fixture_z, sensore della
    # levelboard), con lo stesso limite di discesa che calcolano loro.
    # ------------------------------------------------------------------
    def _tocchi(self, gcmd, tc, tool, etichetta):
        x = tc.station_x + tc.tool_fixture_shift_x
        y = tc.station_y
        valori = []
        for _ in range(int(self.valori['tara_ugello_tocchi'])):
            valori.append(tc._with_motion(
                gcmd, lambda: tc._probe_fixture_z(gcmd, x, y)))
        scarto = max(valori) - min(valori)
        media = sum(valori) / len(valori)
        gcmd.respond_info('testina %d %s: %s -> media %.4f (scarto %.4f)' % (
            tool + 1, etichetta, ' '.join('%.4f' % v for v in valori), media,
            scarto))
        if scarto > self.valori['tara_ugello_scarto_tocchi']:
            raise gcmd.error('testina %d %s: tocchi diversi di %.3f mm (max %.3f): '
                             'punta sporca? Taratura annullata'
                             % (tool + 1, etichetta, scarto,
                                self.valori['tara_ugello_scarto_tocchi']))
        return media

    def _aspetta_freddo(self, gcmd, heater, soglia):
        ora = self._ora()
        fine = ora + 1800.
        prossimo = ora
        while True:
            temp = heater.get_temp(ora)[0]
            if temp <= soglia:
                return
            if self.printer.is_shutdown():
                raise gcmd.error('Stampante in errore')
            if ora >= fine:
                raise gcmd.error('Ugello non sceso a %.0f C in 30 minuti'
                                 % soglia)
            if ora >= prossimo:
                gcmd.respond_info('Raffreddamento ugello: %.0f C (obiettivo '
                                  '%.0f C)' % (temp, soglia))
                prossimo = ora + 60.
            ora = self.reactor.pause(ora + 1.)

    def cmd_tara_ugello(self, gcmd):
        if self._stato_stampa() in ('printing', 'paused'):
            raise gcmd.error('Non durante una stampa')
        if not gcmd.get_int('BUILDPLATE_REMOVED', 0, minval=0, maxval=1):
            raise gcmd.error('Togli il piatto (come per la calibrazione '
                             'offset) e aggiungi BUILDPLATE_REMOVED=1')
        testine = self._testine_da_tarare(gcmd)
        temps = gcmd.get('TEMPS', None)
        temps = ([float(x) for x in temps.split(',') if x.strip()]
                 if temps else list(self.temp_tara_ugello))
        if not temps or min(temps) < 170. or max(temps) > 300.:
            raise gcmd.error('TEMPS tra 170 e 300 C')
        tc = self._toolchanger(gcmd)
        ora = self._ora()
        th = self.printer.lookup_object('toolhead').get_status(ora)
        if th.get('homed_axes', '') != 'xyz':
            raise gcmd.error('Fai l\'homing (C5_HOME_FOR_PRINT) prima di '
                             'togliere il piatto')
        letto = self.pheaters.heaters.get('heater_bed')
        if letto is not None and (letto.target_temp > 0.
                                  or letto.get_temp(ora)[0] > 35.):
            raise gcmd.error('Piatto spento e sotto i 35 C: il piattello '
                             'non deve risentire del piatto')
        guidata = gcmd.get_int('GUIDATA', 0, minval=0, maxval=1)
        try:
            self._tara_ugello_testine(gcmd, tc, testine, temps)
        except Exception as e:
            if guidata:
                self._fine_guidata('NON riuscita: %s' % str(e)[:150])
            raise
        if guidata:
            self._fine_guidata('Riuscita: tabella salvata (RC_TABELLA_Z per '
                               'vederla).')

    def _fine_guidata(self, esito):
        esito = esito.replace('"', "'").replace('|', '/')
        try:
            self.gcode.run_script_from_command(
                '_RC_TU_PASSO PASSO=fine ESITO="%s"' % esito)
        except Exception:
            logging.exception('rc_turbo: finestra fine taratura')

    def _tara_ugello_testine(self, gcmd, tc, testine, temps):
        esiti = {}
        for tool in testine:
            try:
                self._tara_ugello_testina(gcmd, tc, tool, temps)
                esiti[tool] = 'ok'
            except Exception as e:
                if self.printer.is_shutdown() or len(testine) == 1:
                    raise
                esiti[tool] = str(e)
                gcmd.respond_info('testina %d non tarata: %s' % (tool + 1, e))
        if len(testine) > 1:
            riepilogo = ', '.join('testina %d %s' % (t + 1, 'ok' if v == 'ok'
                                               else 'NON tarata')
                                  for t, v in sorted(esiti.items()))
            if any(v != 'ok' for v in esiti.values()):
                raise gcmd.error('Taratura ugelli: ' + riepilogo)
            gcmd.respond_info('Taratura ugelli: ' + riepilogo)

    def _testine_da_tarare(self, gcmd):
        """TOOL=n, TOOLS=0,2 oppure TOOLS=ALL (TUTTE)."""
        testo = gcmd.get('TOOLS', None)
        if testo is None:
            testo = gcmd.get('TOOL', None)
        if testo is None:
            raise gcmd.error('Indica TOOL=n, TOOLS=0,2 o TOOLS=ALL')
        testo = str(testo).strip().upper()
        if testo in ('ALL', 'TUTTE'):
            return [0, 1, 2, 3]
        testine = []
        for parte in testo.replace('T', '').split(','):
            parte = parte.strip()
            if parte not in ('0', '1', '2', '3'):
                raise gcmd.error('Testine valide: 0..3 o ALL')
            if int(parte) not in testine:
                testine.append(int(parte))
        return testine

    def _tara_ugello_testina(self, gcmd, tc, tool, temps):
        ora = self._ora()
        heater = self.pheaters.heaters[heater_of(tool)]
        t_freddo = heater.get_temp(ora)[0]
        if t_freddo > 40.:
            raise gcmd.error('testina %d e\' a %.0f C: aspetta che sia fredda'
                             % (tool + 1, t_freddo))
        if hasattr(tc, '_load_extruder_json'):
            tc._load_extruder_json()
        riferimento = tc.measurements[tool][2]
        run = self.gcode.run_script_from_command
        _, _, montata = tc._preflight(gcmd)
        tc._dock_for_offset_reference(gcmd, montata)
        # Controllo piatto tolto + limite di discesa, come OpenCreator
        pin_z = tc._with_motion(gcmd, lambda: tc._check_plate_removed(gcmd))
        tc._stock_pin_approach_z = pin_z - 5.
        punti = {}
        try:
            run('AFC_SELECT_TOOL TOOL=%s' % heater_of(tool))
            if tc._preflight(gcmd)[2] != tool:
                raise gcmd.error('testina %d non montata' % (tool + 1))
            freddo1 = self._tocchi(gcmd, tc, tool, 'freddo %.0f C'
                                   % t_freddo)
            fabbrica = freddo1 - riferimento
            gcmd.respond_info('testina %d freddo rispetto a extruder.json: %+.4f mm'
                              '%s' % (tool + 1, fabbrica, '' if abs(fabbrica)
                                      <= 0.02 else ' (oltre 0,02: la '
                                      'fabbrica forse misura a caldo, o '
                                      'punta sporca)'))
            # Senza filamento (consigliato) niente spurgo: resta la passata
            # sul silicone, che toglie i residui sciolti dal caldo.
            try:
                con_filamento = self._filamento(tool)
            except Exception:
                con_filamento = False
            gcmd.respond_info('testina %d %s filamento: %s' % (
                tool + 1, 'con' if con_filamento else 'senza',
                'spurgo + pulizia prima di ogni tocco' if con_filamento
                else 'solo pulizia prima di ogni tocco'))
            # Dal piu' caldo al piu' freddo: una sola salita, poi gradini in
            # discesa, e il raffreddamento finale parte dalla temperatura bassa.
            for temp in sorted(temps, reverse=True):
                run('_RC_TARA_UGELLO_SCALDA T=%d TEMP=%.0f SOAK=%.0f '
                    'SPURGO=%d' % (tool, temp,
                                   self.valori['tara_ugello_soak_s'],
                                   1 if con_filamento else 0))
                punti[temp] = self._tocchi(gcmd, tc, tool, '%.0f C' % temp)
            run('_RC_TARA_UGELLO_RAFFREDDA T=%d' % tool)
            self._aspetta_freddo(gcmd, heater, t_freddo
                                 + self.valori['tara_ugello_margine_freddo'])
            run('SET_FAN_SPEED FAN=fanM106 SPEED=0')
            t_finale = heater.get_temp(self._ora())[0]
            freddo2 = self._tocchi(gcmd, tc, tool, 'freddo finale %.0f C'
                                   % t_finale)
        finally:
            tc._stock_pin_approach_z = None
            try:
                run('SET_FAN_SPEED FAN=fanM106 SPEED=0')
                run('_RC_TARA_Z_FINE')
            except Exception:
                logging.exception('rc_turbo: fine taratura ugello')
        deriva = freddo2 - freddo1
        if abs(deriva) > self.valori['tara_ugello_scarto_freddo']:
            raise gcmd.error('Freddo iniziale e finale diversi di %.3f mm: '
                             'qualcosa si e\' mosso, taratura scartata'
                             % deriva)
        freddo = (freddo1 + freddo2) / 2.
        tabella = {round((t_freddo + t_finale) / 2., 1):
                   freddo - riferimento}
        for temp, z in punti.items():
            tabella[temp] = z - riferimento
        fuori = [t for t, v in tabella.items() if abs(v) > LIMITE_TABELLA]
        if fuori:
            raise gcmd.error('Valori oltre %.2f mm a %s C: non salvati'
                             % (LIMITE_TABELLA, fuori))
        self.tabella_ugello[tool] = {
            'riferimento': riferimento, 'punti': tabella,
            'data': time.strftime('%Y-%m-%d %H:%M'),
            'ore': round(self.ore.get(tool, 0.), 1)}
        self.avvisi_ugello.discard(tool)
        self._salva_tabella_z()
        coeff = 0.00045
        try:
            dati, _ = tc._read_stock_json(tc.test_json_path)
            coeff = float(dati['tempOffset'])
        except Exception:
            pass
        righe = ['%.0f C: allungamento %+.3f mm (formula %+.3f)'
                 % (t, punti[t] - freddo, (t - 120.) * coeff)
                 for t in sorted(punti)]
        gcmd.respond_info('Tabella ugello testina %d salvata:\n%s'
                          % (tool + 1, '\n'.join(righe)))

    # ------------------------------------------------------------------
    # Stato
    # ------------------------------------------------------------------
    def cmd_stato(self, gcmd):
        cf = self.printer.lookup_object('configfile')
        s = cf.get_status(self._ora()).get('settings', {})
        massimi = ', '.join('%s %s' % (n, s.get(n, {}).get('max_temp', '?'))
                            for n in ('extruder', 'heater_bed',
                                      'heater_generic ' + self.camera_nome))
        righe = [
            'limite ugelli: %s (max %d)' % (self.limite_ugelli,
                                            self.max_ugelli),
            'guardia camera: %s' % self.guardia_camera,
            'spurgo prima presa: %s, torre: %s' % (
                self.spurgo_prima_presa, 'attiva' if self._torre()
                else 'spenta'),
            'carico: %s' % self.carico,
            'tabella Z piatto: %s (%s)' % (self.tabella_z_piatto,
                                          self._testo_tabella()),
            'tabella Z ugello: %s (%s)' % (self.tabella_z_ugello,
                                          self._testo_ugello()),
            'mesh di riserva: %s ("%s")' % (self.riserva_mesh,
                                            self.mesh_riserva),
            'ripresa dopo il cambio: %s (%s; fatte in questa stampa: %d)' % (
                self.ripresa_cambio, ', '.join('%s +%g mm' % kv for kv in
                                               sorted(self.ripresa_extra.items())),
                self.riprese_fatte),
            'tempi: %s' % self.tempi,
            'temperature massime: ' + massimi,
            'ugelli in coda: %s' % (', '.join(self.in_coda) or 'nessuno'),
        ]
        gcmd.respond_info('Creator Turbo\n' + '\n'.join(righe))

    def get_status(self, eventtime=None):
        stato = dict(self.valori)
        stato.update({
            'mesh_profilo_riserva': self.mesh_riserva,
            'temperature_materiali': dict(self.materiali),
            'spurgo_prima_presa': self.spurgo_prima_presa,
            'carico': self.carico,
            'torre': self._torre(),
            'pronte': sorted(self.pronte),
            'in_partenza': self.in_partenza,
            'taratura_attiva': self.taratura is not None,
            'tabella_z': {('%g' % k): v for k, v in self.tabella_z.items()},
            'tabella_ugello_testine': sorted(self.tabella_ugello),
            'in_coda': list(self.in_coda),
            'materiali_file': list(self.materiali_file),
            'riprese_fatte': self.riprese_fatte,
            'temp_testine': {str(k): v for k, v in self.temp_testine.items()},
        })
        return stato


def load_config(config):
    return RCTurbo(config)
