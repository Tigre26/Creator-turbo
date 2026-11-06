# Prove a secco di Creator Turbo (senza stampante).
#   python3 tests/test_rc_turbo.py            logica del modulo
#   KALICO=/percorso/kalico python3 tests/test_rc_turbo.py   anche le macro
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rc_turbo",
                                              ROOT / "klippy" / "rc_turbo.py")
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)


class FakeReactor:
    NOW = 0.

    def __init__(self):
        self.t = 1000.
        self.timers = []

    def monotonic(self):
        return self.t

    def pause(self, until):
        self.t = max(self.t, until)
        for cb in list(self.timers):
            cb(self.t)
        return self.t

    def register_timer(self, cb, when):
        self.timers.append(cb)


class FakeHeater:
    def __init__(self, name, temp=25., max_power=1.):
        self.name, self.temp, self.target_temp = name, temp, 0.
        self.min_temp, self.max_temp = 0., 320.
        self.last_pwm_value, self.max_power = 0., max_power

    def get_name(self):
        return self.name

    def get_temp(self, eventtime):
        return self.temp, self.target_temp

    def set_temp(self, t):
        self.target_temp = t

    def get_max_power(self):
        return self.max_power


class FakeConfig:
    def __init__(self, printer, values=None):
        self.printer, self.values = printer, values or {}

    def get_printer(self):
        return self.printer

    def _get(self, key, default):
        return self.values.get(key, default)

    get = _get

    def getboolean(self, key, default):
        return self._get(key, default)

    def getint(self, key, default, **kw):
        return int(self._get(key, default))

    def getfloat(self, key, default, **kw):
        v = self._get(key, default)
        return None if v is None else float(v)

    def error(self, msg):
        return Exception(msg)


def make(values=None, cartella=None):
    reactor = FakeReactor()
    gcode = mock.Mock()
    gcode.register_command.return_value = None
    heaters = mock.Mock()
    heaters.heaters = {n: FakeHeater(n) for n in rc.EXTRUDERS}
    heaters.heaters['chamber_heater'] = FakeHeater('heater_generic chamber_heater',
                                                   max_power=.8)
    applied = []

    def orig_set(heater, temp, wait=False):
        applied.append((heater.name, temp, wait))
        heater.set_temp(temp)
    heaters.set_temperature = orig_set
    configfile = mock.Mock()
    configfile.get_status.return_value = {'settings': {
        'verify_heater chamber_heater': {'hysteresis': 5.}}}
    objects = {'gcode': gcode, 'heaters': heaters, 'configfile': configfile,
               'toolhead': mock.Mock()}
    printer = mock.Mock()
    printer.get_reactor.return_value = reactor
    printer.is_shutdown.return_value = False
    printer.lookup_object.side_effect = lambda n, d=None: objects.get(n, d)
    printer.get_start_args.return_value = {
        'config_file': os.path.join(cartella or tempfile.mkdtemp(),
                                    'printer.cfg')}
    turbo = rc.load_config(FakeConfig(printer, values))
    turbo._pronto()
    return turbo, reactor, heaters.heaters, applied, gcode


class Funzioni(unittest.TestCase):
    def test_interpola(self):
        p = {25.: 0., 60.: -0.02, 110.: -0.06}
        self.assertEqual(rc.interpola(p, 10), 0.)
        self.assertAlmostEqual(rc.interpola(p, 85), -0.04)
        self.assertEqual(rc.interpola(p, 120), -0.06)
        self.assertIsNone(rc.interpola({}, 50))

    def test_torre(self):
        self.assertTrue(rc.torre_da_testo("; enable_prime_tower = 1\n"))
        self.assertFalse(rc.torre_da_testo("; enable_prime_tower = 0\n"))
        self.assertIsNone(rc.torre_da_testo("; altro = 1\n"))

    def test_materiali(self):
        t = rc.leggi_materiali("DEFAULT:220, pla:220,\n ASA:250")
        self.assertEqual(t, {'DEFAULT': 220., 'PLA': 220., 'ASA': 250.})


class LimiteUgelli(unittest.TestCase):
    def test_terzo_in_coda_poi_parte(self):
        turbo, reactor, h, applied, _ = make()
        turbo.pheaters.set_temperature(h['extruder'], 220.)
        turbo.pheaters.set_temperature(h['extruder1'], 220.)
        turbo.pheaters.set_temperature(h['extruder2'], 220.)
        self.assertEqual(h['extruder2'].target_temp, 0.)
        self.assertIn('extruder2', turbo.in_coda)
        h['extruder'].temp = 216.          # entro 5 C: non conta piu'
        reactor.pause(reactor.t + 1)
        self.assertEqual(h['extruder2'].target_temp, 220.)
        self.assertFalse(turbo.in_coda)

    def test_abbassare_e_spegnere_subito(self):
        turbo, reactor, h, applied, _ = make()
        for n in ('extruder', 'extruder1'):
            turbo.pheaters.set_temperature(h[n], 250.)
        h['extruder2'].temp = 200.
        turbo.pheaters.set_temperature(h['extruder2'], 150.)   # raffredda
        self.assertEqual(h['extruder2'].target_temp, 150.)
        turbo.pheaters.set_temperature(h['extruder3'], 220.)
        self.assertIn('extruder3', turbo.in_coda)
        turbo.pheaters.set_temperature(h['extruder3'], 0.)
        self.assertNotIn('extruder3', turbo.in_coda)

    def test_attesa_m109_in_coda(self):
        turbo, reactor, h, applied, _ = make()
        for n in ('extruder', 'extruder1'):
            turbo.pheaters.set_temperature(h[n], 250.)

        def scalda(t):
            h['extruder'].temp = 250.
        reactor.timers.insert(0, scalda)
        turbo.pheaters.set_temperature(h['extruder2'], 240., True)
        self.assertEqual(applied[-1], ('extruder2', 240., True))


class GuardiaCamera(unittest.TestCase):
    def test_abbassa_su_calo_lento(self):
        turbo, reactor, h, _, gcode = make()
        cam = h['chamber_heater']
        cam.set_temp(65.)
        cam.temp = 64.
        reactor.pause(reactor.t + 2)      # raggiunto
        cam.last_pwm_value = .8           # al massimo
        for i in range(240):
            cam.temp = 64. - i * 0.03     # ~1,8 C/min
            reactor.pause(reactor.t + 1)
        self.assertLess(cam.target_temp, 65.)
        self.assertGreaterEqual(cam.target_temp, 55.)
        self.assertLessEqual(cam.temp, cam.target_temp)
        self.assertGreaterEqual(cam.temp, cam.target_temp - 5)

    def test_non_tocca_su_calo_rapido(self):
        turbo, reactor, h, _, _ = make()
        cam = h['chamber_heater']
        cam.set_temp(65.)
        cam.temp = 64.
        reactor.pause(reactor.t + 2)
        cam.last_pwm_value = .8
        for i in range(90):
            cam.temp = 64. - i * 0.2      # 12 C/min: sonda sospetta
            reactor.pause(reactor.t + 1)
        self.assertEqual(cam.target_temp, 65.)

    def test_non_tocca_in_salita(self):
        turbo, reactor, h, _, _ = make()
        cam = h['chamber_heater']
        cam.set_temp(65.)
        cam.last_pwm_value = .8
        for i in range(120):
            cam.temp = 30. + i * 0.1
            reactor.pause(reactor.t + 1)
        self.assertEqual(cam.target_temp, 65.)


class TabellaZ(unittest.TestCase):
    def test_sostituisce_solo_il_piatto(self):
        def orig(hotend, coeff, bed, fl):
            return ((hotend - 120.) * coeff - (0.08 if bed >= 100. else 0.)
                    - (0.06 if fl < 0.11 else 0.))
        cartella = tempfile.mkdtemp()
        with open(os.path.join(cartella, rc.TABELLA_Z), 'w') as f:
            json.dump({'piatto': {'25': 0., '110': -0.05}}, f)
        turbo, *_ = make({'tabella_z_piatto': True}, cartella)
        c = turbo._correzione_z(orig, 250., 0.00045, 110., 0.2)
        self.assertAlmostEqual(c, 130 * 0.00045 - 0.05)
        turbo.tabella_z_piatto = False
        c = turbo._correzione_z(orig, 250., 0.00045, 110., 0.2)
        self.assertAlmostEqual(c, 130 * 0.00045 - 0.08)
        turbo.taratura = {'tool': 0}
        c = turbo._correzione_z(orig, 250., 0.00045, 110., 0.2)
        self.assertAlmostEqual(c, 130 * 0.00045)

    def test_piatto_caldo_regolabile(self):
        def orig(hotend, coeff, bed, fl):
            return ((hotend - 120.) * coeff - (0.08 if bed >= 100. else 0.)
                    - (0.06 if fl < 0.11 else 0.))
        turbo, *_ = make({'piatto_caldo_mm': -0.04})
        self.assertAlmostEqual(turbo._correzione_z(orig, 265., 0.00045, 110.,
                                                   0.2), 145 * 0.00045 - 0.04)
        self.assertAlmostEqual(turbo._correzione_z(orig, 220., 0.00045, 60.,
                                                   0.2), 100 * 0.00045)


class TabellaUgello(unittest.TestCase):
    def orig(self, hotend, coeff, bed, fl):
        return ((hotend - 120.) * coeff - (0.08 if bed >= 100. else 0.)
                - (0.06 if fl < 0.11 else 0.))

    def prepara(self, riferimento=1.406):
        turbo, reactor, h, applied, gcode = make({'tabella_z_ugello': True})
        tc = mock.Mock()
        tc.measurements = [[0., 0., 1.4], [0., 0., 1.5], [0., 0., riferimento],
                           [0., 0., 1.4]]
        turbo.printer.lookup_object.side_effect = (
            lambda n, d=None: tc if n == 'creator5_toolchanger' else
            {'gcode': gcode, 'heaters': turbo.pheaters,
             'toolhead': mock.Mock()}.get(n, d))
        turbo.tabella_ugello = {2: {'riferimento': 1.406,
                                    'punti': {25.: 0., 220.: 0.03,
                                              260.: 0.05}, 'data': ''}}
        return turbo, tc

    def test_sostituisce_la_stima(self):
        turbo, tc = self.prepara()
        turbo.testina_z = 2
        c = turbo._correzione_z(self.orig, 240., 0.00045, 60., 0.2)
        self.assertAlmostEqual(c, 0.04)          # interpolata, niente formula
        turbo.testina_z = 1                      # T1 senza tabella
        c = turbo._correzione_z(self.orig, 240., 0.00045, 60., 0.2)
        self.assertAlmostEqual(c, 120 * 0.00045)

    def test_tabella_vecchia_ignorata(self):
        turbo, tc = self.prepara(riferimento=1.420)
        turbo.testina_z = 2
        c = turbo._correzione_z(self.orig, 240., 0.00045, 60., 0.2)
        self.assertAlmostEqual(c, 120 * 0.00045)

    def test_piano_una_testina_alla_volta(self):
        turbo, tc = self.prepara()
        visti = []

        def vecchio(gcmd):
            p = gcmd.params
            tc.print_z_corrections = [None] * 4
            for t in range(4):
                if 'HOTEND%d' % t in p:
                    visti.append((t, turbo.testina_z))
                    tc.print_z_corrections[t] = turbo._correzione_z(
                        self.orig, float(p['HOTEND%d' % t]), 0.00045,
                        60., 0.2)
        turbo.gcode.create_gcode_command.side_effect = (
            lambda c, l, p: mock.Mock(params=p))
        gcmd = mock.Mock()
        gcmd.get_command_parameters.return_value = {
            'HOTEND0': '220', 'HOTEND2': '260', 'BED': '60'}
        turbo._attorno_piano_z(vecchio, gcmd)
        self.assertEqual(visti, [(0, 0), (2, 2)])
        self.assertAlmostEqual(tc.print_z_corrections[2], 0.05)
        self.assertAlmostEqual(tc.print_z_corrections[0], 100 * 0.00045)
        self.assertIsNone(tc.print_z_corrections[1])


class CorrezioneZTestina(unittest.TestCase):
    def prepara(self, stato='standby'):
        turbo, reactor, h, applied, gcode = make()
        tc = mock.Mock(busy=False)
        tc.z_adjustments = [0., 0., 0., 0.01]
        scritti = []

        def scrivi(gcmd, t, v, backup=True):
            scritti.append((t, v))
            tc.z_adjustments[t] = v
        tc._write_z_adjustment.side_effect = scrivi
        stats = mock.Mock()
        stats.get_status.return_value = {'state': stato}
        turbo.printer.lookup_object.side_effect = (
            lambda n, d=None: {'creator5_toolchanger': tc,
                               'print_stats': stats}.get(n, d))
        return turbo, tc, scritti

    def gcmd(self, **p):
        g = mock.Mock()
        g.get_int.side_effect = lambda k, d=None, **kw: int(p.get(k, d))
        g.get_float.side_effect = (
            lambda k, d=None, **kw: None if p.get(k, d) is None
            else float(p.get(k, d)))
        g.error.side_effect = Exception
        return g

    def test_aggiungi_testina_4(self):
        turbo, tc, scritti = self.prepara()
        turbo.cmd_z_testina(self.gcmd(TESTINA=4, AGGIUNGI=-0.02))
        self.assertEqual(scritti[0][0], 3)          # testina 4 = T3
        self.assertAlmostEqual(scritti[0][1], -0.01)

    def test_valore_fisso_e_limite(self):
        turbo, tc, scritti = self.prepara()
        turbo.cmd_z_testina(self.gcmd(TESTINA=1, Z=-0.02))
        self.assertAlmostEqual(tc.z_adjustments[0], -0.02)
        with self.assertRaises(Exception):
            turbo.cmd_z_testina(self.gcmd(TESTINA=1, Z=-0.5))

    def test_rifiuta_in_stampa(self):
        turbo, tc, scritti = self.prepara('printing')
        with self.assertRaises(Exception):
            turbo.cmd_z_testina(self.gcmd(TESTINA=4, AGGIUNGI=-0.02))
        self.assertEqual(scritti, [])

    def test_solo_elenco(self):
        turbo, tc, scritti = self.prepara('printing')
        turbo.cmd_z_testina(self.gcmd())
        self.assertEqual(scritti, [])


class CambioTestina(unittest.TestCase):
    def test_stessa_testina_non_conta(self):
        turbo, reactor, h, applied, gcode = make({'tempi': True})
        turbo._stato_stampa = lambda: 'printing'
        turbo._temp_stampa = lambda t: 220.
        turbo.torre_file, turbo.torre_forzata = False, None
        turbo.ultima_testina, turbo.pronte, turbo.cambi = 2, {2}, []
        gcode.run_script_from_command.reset_mock()

        def g(t):
            m = mock.Mock()
            m.get_int.side_effect = lambda k, d=None, **kw: t
            return m
        turbo._attorno_cambio(lambda x: None, g(2))     # gia' montata
        self.assertEqual(turbo.cambi, [])
        gcode.run_script_from_command.assert_not_called()
        turbo._attorno_cambio(lambda x: None, g(3))     # cambio vero
        self.assertEqual(len(turbo.cambi), 1)
        self.assertIn('LENGTH=25', gcode.run_script_from_command.call_args[0][0])
        turbo._attorno_cambio(lambda x: None, g(3))     # di nuovo la stessa
        self.assertEqual(len(turbo.cambi), 1)


class PartenzaEFine(unittest.TestCase):
    def test_preriscaldo_solo_testine_a_100(self):
        turbo, reactor, h, applied, gcode = make(
            {'tempi': False, 'preriscaldo_partenza': True})
        for n, t in (('extruder', 100.), ('extruder1', 0.),
                     ('extruder2', 100.)):
            x = FakeHeater(n)
            x.target_temp = t
            turbo.pheaters.heaters[n] = x
        turbo.temp_testine = {0: 220., 1: 220., 2: 140., 3: 220.}
        turbo.in_partenza = True
        visti = []
        turbo.pheaters.set_temperature = lambda h_, t, wait=False: \
            visti.append((h_.name, t))
        turbo._attorno_homing(lambda g: None, mock.Mock())
        self.assertEqual(visti, [('extruder', 150.), ('extruder2', 140.)])

    def test_preriscaldo_spento_o_fuori_partenza(self):
        turbo, reactor, h, applied, gcode = make({'tempi': False})
        x = FakeHeater('extruder')
        x.target_temp = 100.
        turbo.pheaters.heaters['extruder'] = x
        turbo.temp_testine = {0: 220.}
        turbo.in_partenza = True
        turbo.pheaters.set_temperature = mock.Mock()
        turbo._attorno_homing(lambda g: None, mock.Mock())
        turbo.pheaters.set_temperature.assert_not_called()

    def prova_piatto(self, z):
        turbo, reactor, h, applied, gcode = make({'piatto_giu_fine': True})
        tc = mock.Mock(lower_bed_z=246.)
        th = mock.Mock()
        th.get_position.return_value = [0., 0., z, 0.]
        turbo.printer.lookup_object.side_effect = (
            lambda n, d=None: {'creator5_toolchanger': tc,
                               'toolhead': th}.get(n, d))
        visto = []
        turbo._attorno_piatto_giu(lambda g: visto.append(tc.lower_bed_z),
                                  mock.Mock())
        return visto[0], tc.lower_bed_z

    def test_piatto_giu_50(self):
        self.assertEqual(self.prova_piatto(10.), (60., 246.))

    def test_piatto_giu_pezzo_alto(self):
        self.assertEqual(self.prova_piatto(220.), (246., 246.))


class AttesaPiatto(unittest.TestCase):
    def prova(self, target, params=None):
        turbo, reactor, h, applied, gcode = make({'tempi': False})
        tc = mock.Mock(bed_soak_minutes=0.5)
        bed = FakeHeater('heater_bed')
        bed.target_temp = target
        turbo.pheaters.heaters['heater_bed'] = bed
        turbo.printer.lookup_object.side_effect = (
            lambda n, d=None: tc if n == 'creator5_toolchanger' else d)
        visto = []
        gcmd = mock.Mock()
        gcmd.get_command_parameters.return_value = params or {}
        turbo._attorno_attesa_piatto(
            lambda g: visto.append(tc.bed_soak_minutes), gcmd)
        return visto[0], tc.bed_soak_minutes

    def test_pla_30s(self):
        self.assertEqual(self.prova(60.), (0.5, 0.5))

    def test_asa_90s_poi_ripristina(self):
        self.assertEqual(self.prova(110.), (1.5, 0.5))

    def test_minutes_esplicito_rispettato(self):
        self.assertEqual(self.prova(110., {'MINUTES': '5'}), (0.5, 0.5))


class ProceduraTaraUgello(unittest.TestCase):
    def esegui(self, deriva=0., rumore=0., vals=None):
        cartella = tempfile.mkdtemp()
        turbo, reactor, h, applied, gcode = make({}, cartella)
        h['heater_bed'] = FakeHeater('heater_bed')
        ugello = h['extruder1']
        tc = mock.Mock()
        tc.station_x, tc.station_y, tc.tool_fixture_shift_x = 28.8, 215.1, -12.5
        tc.measurements = [[0., 0., 1.4], [0., 0., 1.500], [0., 0., 1.4],
                           [0., 0., 1.500]]
        tc._with_motion.side_effect = lambda g, f, **k: f()
        tc._check_plate_removed.return_value = 5.
        stato_sel = {'t': None}
        tc._preflight.side_effect = lambda g: (None, None, stato_sel['t'])
        tc._read_stock_json.return_value = ({'tempOffset': 0.00045}, '')
        stato = {'n': 0, 'freddi': 0}
        ugelli = h

        def tocco(g, x, y):
            ugello = ugelli[rc.heater_of(stato_sel['t'])]
            stato['n'] += 1
            z = 1.500 + (ugello.temp - 25.) * 0.0002
            if ugello.temp < 40. and stato['freddi'] >= 3:
                z += deriva
            if ugello.temp < 40.:
                stato['freddi'] += 1
            return z + (rumore if stato['n'] % 2 else 0.)
        tc._probe_fixture_z.side_effect = tocco
        objects = {'gcode': gcode, 'heaters': turbo.pheaters,
                   'creator5_toolchanger': tc, 'print_stats': None}
        th = mock.Mock()
        th.get_status.return_value = {'homed_axes': 'xyz'}
        objects['toolhead'] = th
        turbo.printer.lookup_object.side_effect = (
            lambda n, d=None: objects.get(n, d))

        def script(cmd):
            ugello = ugelli[rc.heater_of(stato_sel['t'])] \
                if stato_sel['t'] is not None else None
            if cmd.startswith('AFC_SELECT_TOOL'):
                nome = cmd.split('TOOL=')[1].strip()
                stato_sel['t'] = rc.tool_of(nome)
                stato['freddi'] = 0
            elif cmd.startswith('_RC_TARA_Z_FINE'):
                stato_sel['t'] = None
            elif cmd.startswith('_RC_TARA_UGELLO_SCALDA'):
                ugello.temp = float(cmd.split('TEMP=')[1].split()[0])
            elif cmd.startswith('_RC_TARA_UGELLO_RAFFREDDA'):
                ugello.temp = 28.
        gcode.run_script_from_command.side_effect = script
        gcmd = mock.Mock()
        vals = vals or {'TOOL': 1, 'BUILDPLATE_REMOVED': 1}
        gcmd.get_int.side_effect = lambda k, d=None, **kw: vals.get(k, d)
        gcmd.get.side_effect = lambda k, d=None: vals.get(k, d)
        self.risposte = []
        gcmd.respond_info.side_effect = self.risposte.append
        gcmd.error.side_effect = lambda m: Exception(m)
        turbo.cmd_tara_ugello(gcmd)
        return turbo, tc

    def test_tabella_salvata(self):
        turbo, tc = self.esegui()
        punti = turbo.tabella_ugello[1]['punti']
        self.assertAlmostEqual(punti[220.], 195 * 0.0002, places=6)
        self.assertAlmostEqual(punti[300.], 275 * 0.0002, places=6)
        self.assertAlmostEqual(punti[26.5], 1.5 * 0.0002, places=6)
        self.assertIsNone(tc._stock_pin_approach_z)   # limite ripristinato
        self.assertEqual(tc._probe_fixture_z.call_count, 15)

    def test_piu_testine_e_ore(self):
        turbo, tc = self.esegui(vals={'TOOLS': '1,3', 'BUILDPLATE_REMOVED': 1})
        self.assertEqual(sorted(turbo.tabella_ugello), [1, 3])
        self.assertTrue(any('testina 2 ok, testina 4 ok' in r for r in self.risposte))
        turbo.ore = {1: 600.}
        turbo.tabella_ugello[1]['ore'] = 50.
        turbo._info = mock.Mock()
        turbo._promemoria_taratura({1, 3})
        self.assertEqual(turbo._info.call_count, 1)
        self.assertIn('testina 2: 550 ore', turbo._info.call_args[0][0])

    def test_scelta_testine(self):
        turbo, *_ = make()
        g = mock.Mock()
        g.error.side_effect = lambda m: Exception(m)
        for testo, atteso in (('ALL', [0, 1, 2, 3]), ('0,2', [0, 2]),
                              ('T3', [3])):
            g.get.side_effect = lambda k, d=None, t=testo: (
                t if k == 'TOOLS' else d)
            self.assertEqual(turbo._testine_da_tarare(g), atteso)

    def test_scarta_deriva(self):
        with self.assertRaises(Exception) as e:
            self.esegui(deriva=0.02)
        self.assertIn('scartata', str(e.exception))

    def test_scarta_tocchi_diversi(self):
        with self.assertRaises(Exception) as e:
            self.esegui(rumore=0.03)
        self.assertIn('punta sporca', str(e.exception))


class Manifest(unittest.TestCase):
    def test_file(self):
        m = json.loads((ROOT / "opencreator-plugin.json").read_text())
        for rel in m["klippy_extras"] + [m["klipper_config"]]:
            self.assertTrue((ROOT / rel).is_file(), rel)


@unittest.skipUnless(os.environ.get('KALICO'), 'KALICO non impostato')
class Macro(unittest.TestCase):
    """Config letta con il parser di Kalico, macro compilate con il suo Jinja."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.environ['KALICO'], 'klippy'))
        import configparser
        import jinja2
        cls.cp = configparser.RawConfigParser(strict=False)
        cls.cp.read(ROOT / 'config' / 'creator-turbo.cfg')
        cls.env = jinja2.Environment('{%', '%}', '{', '}',
                                     extensions=['jinja2.ext.do',
                                                 'jinja2.ext.loopcontrols'])

    def macro(self, nome):
        return self.env.from_string(self.cp.get('gcode_macro ' + nome,
                                                'gcode'))

    def test_tutte_compilano(self):
        for sez in self.cp.sections():
            if sez.startswith('gcode_macro '):
                self.macro(sez.split(' ', 1)[1])

    def printer(self):
        turbo, *_ = make({'temperature_materiali': self.cp.get(
            'rc_turbo', 'temperature_materiali')})
        stato = turbo.get_status()
        stato['carico'] = True
        settings = {
            'creator5_toolchanger': {'prep_approach_x': 250.,
                                     'prep_approach_y': 250.,
                                     'cooldown_x': 266.5, 'cooldown_y': 13.8,
                                     'cooldown_wipe_z': 1.7},
            'bed_mesh': {'mesh_min': [0., 0.], 'mesh_max': [250., 250.]},
            'probe': {'x_offset': -13., 'y_offset': -4.}}
        return {
            'rc_turbo': stato, 'configfile': {'settings': settings},
            'toolhead': {'axis_maximum': {'z': 300.},
                         'position': {'z': 40.}, 'extruder': 'extruder1',
                         'homed_axes': 'xyz'},
            'gcode_move': {'gcode_position': {'z': 40.2}},
            'extruder1': {'target': 240.},
            'fan_generic fanM106': {'speed': 0.4},
            'print_stats': {'state': 'standby'},
            'creator5_toolchanger': {'active_tool': None},
            'heater_bed': {'target': 60.},
            'AFC': {'lanes': ['extruder0', 'extruder1']},
            'AFC_lane extruder1': {'extruder': 'extruder1',
                                   'material': 'PETG'},
            'AFC_lane extruder0': {'extruder': 'extruder0', 'material': ''},
        }

    def render(self, nome, params, printer=None):
        def errore(msg):
            raise RuntimeError(msg)
        p = printer or self.printer()

        class P(dict):
            def __contains__(self, k):
                return dict.__contains__(self, k)
        return self.macro(nome).render(
            printer=P(p), params=params, rawparams='',
            action_raise_error=errore, action_respond_info=lambda m: '')

    def test_pulisci_non_scende_in_stampa(self):
        out = self.render('_RC_PULISCI', {})
        self.assertIn('G1 Z40.2 F600', out)        # esce alla quota del pezzo
        self.assertIn('G1 X266.5 F3000', out)
        self.assertIn('SET_FAN_SPEED FAN=fanM106 SPEED=0.4', out)  # ripristino

    def test_carica_chiede_materiale(self):
        out = self.render('RC_CARICA', {'TOOL': '1'})
        self.assertNotIn('_RC_CARICA_ESEGUI', out)       # prima chiede
        self.assertIn('action:prompt_begin Carica testina 2', out)
        self.assertIn('PETG (270 C)|RC_CARICA TOOL=1 MATERIAL=PETG SCELTO=1'
                      '|primary', out)                   # quello di prima
        self.assertIn('ASA (280 C)|RC_CARICA TOOL=1 MATERIAL=ASA SCELTO=1'
                      '|secondary', out)
        self.assertIn('action:prompt_show', out)

    def test_carica_dopo_la_scelta(self):
        out = self.render('RC_CARICA', {'TOOL': '1', 'MATERIAL': 'ASA',
                                        'SCELTO': '1'})
        self.assertIn('SET_MATERIAL LANE=extruder1 MATERIAL=ASA', out)
        self.assertIn('_RC_CARICA_ESEGUI TOOL=1 TEMP=280.0', out)  # 250+30
        out = self.render('RC_CARICA', {'TOOL': '0', 'MATERIAL': 'PLA'})
        self.assertNotIn('SET_MATERIAL', out)
        self.assertIn('TEMP=250.0', out)                          # 220+30
        out = self.render('RC_CARICA', {'TOOL': '0', 'TEMP': '290'})
        self.assertIn('TEMP=300.0', out)                          # limite
        self.assertNotIn('prompt_begin', out)

    def test_quadrato(self):
        out = self.render('_RC_QUADRATO', {'X': '110', 'Y': '110',
                                           'RECUPERO': '5'})
        righe = [r.strip() for r in out.splitlines() if r.strip()]
        self.assertIn('G1 Z0.2 F600', righe)
        lunghe = [r for r in righe if r.startswith('G1 X') and ' Y' in r
                  and ' E' in r]
        self.assertEqual(len(lunghe), 60)
        e = float(lunghe[0].split(' E')[1].split()[0])
        self.assertAlmostEqual(e, 30 * 0.5 * 0.2 / (3.14159265 * 0.875 ** 2),
                               places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
