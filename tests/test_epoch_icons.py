"""Regressions for the 3.5.2 fixes.

* Cursor reports its billing cycle in milliseconds, but QuotaItem.reset_at is
  an absolute Unix timestamp in seconds for every provider, and
* the launcher executable must carry the application icon as a real win32
  resource, not merely ship app.ico beside it.
"""

import json
import struct
import time
import unittest
from tests.tk_support import destroy_root
from pathlib import Path
from unittest.mock import patch

import providers as p
import usage_widget as u
import widget_raster as raster

PROJECT = Path(__file__).resolve().parent.parent


class FakeAuth:
    plan = 'pro'

    def load(self): pass
    def ensure_fresh(self): pass
    def access_token(self): return 'fake'
    def account_id(self): return ''


def cursor(billing_cycle_end, **plan_usage):
    usage = {'planUsage': dict({'autoPercentUsed': 35, 'apiPercentUsed': 10}, **plan_usage),
             'billingCycleEnd': billing_cycle_end}
    with patch.object(p, 'CursorAuth', FakeAuth), \
         patch.object(p, 'http_json', return_value=(200, usage)), \
         patch.object(p, '_cursor_plan_name', return_value='Pro'):
        return p.fetch_cursor()


def gpt(reset_at):
    body = {'rate_limit': {'primary_window': {'used_percent': 20, 'limit_window_seconds': 18000,
                                              'reset_at': reset_at}}}
    return p.chatgpt_snapshot(body)


def claude(resets_at, now):
    body = {'rate_limits': {'limits': [{'kind': 'session', 'percent': 20, 'resets_at': resets_at}]}}
    line = json.dumps({'type': 'control_response',
                       'response': {'subtype': 'success', 'request_id': 'usage', 'response': body}})
    return p.claude_usage_from_control_output(line, now)


class EpochHelperTests(unittest.TestCase):
    def test_seconds_are_left_alone(self):
        self.assertEqual(p.normalize_epoch_seconds(1790562343), 1790562343)
        self.assertEqual(p.normalize_epoch_seconds(1790562343.5), 1790562343.5)
        self.assertEqual(p.normalize_epoch_seconds('1790562343'), 1790562343)

    def test_a_millisecond_stamp_becomes_seconds(self):
        self.assertAlmostEqual(p.normalize_epoch_seconds(1790562343024), 1790562343.024, places=3)
        self.assertAlmostEqual(p.normalize_epoch_seconds('1790562343024'), 1790562343.024, places=3)

    def test_nothing_usable_is_never_guessed_at(self):
        for value in (None, True, False, float('nan'), float('inf'), float('-inf'),
                      'later', '', '   ', [], {}, object()):
            with self.subTest(value=repr(value)):
                self.assertIsNone(p.normalize_epoch_seconds(value))

    def test_only_providers_that_send_millis_are_normalized(self):
        millis = 1790562343024
        self.assertAlmostEqual(p.epoch_for_source('cursor', millis), 1790562343.024, places=3)
        self.assertAlmostEqual(p.epoch_for_source('CURSOR', millis), 1790562343.024, places=3)
        # Everyone else is passed through: dividing an unknown provider's
        # timestamp would be the same guess this exists to avoid.
        for source in ('chatgpt', 'claude', 'claude_statusline', '', None, 'future-provider'):
            with self.subTest(source=source):
                self.assertEqual(p.epoch_for_source(source, millis), float(millis))


class CursorResetTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        # An hour past the day boundary, so millisecond truncation in the
        # fixture cannot flip the floor-divided countdown to 7 days.
        self.eight_days = self.now + 8 * 86400 + 3600

    def test_the_real_millisecond_string_lands_as_seconds(self):
        snap = cursor(str(int(self.eight_days * 1000)))
        for item in snap.main_limits:
            with self.subTest(quota=item.raw_identifier):
                self.assertAlmostEqual(item.reset_at, self.eight_days, delta=1)

    def test_a_seconds_value_is_still_accepted_unchanged(self):
        snap = cursor(int(self.eight_days))
        self.assertAlmostEqual(snap.main_limits[0].reset_at, self.eight_days, delta=1)

    def test_the_countdown_is_a_realistic_span(self):
        snap = cursor(str(int(self.eight_days * 1000)))
        hero = snap.main_limits[0]
        self.assertEqual(u.reset_countdown(hero.reset_at, self.now, True), '8일 후')
        # The number that made this bug visible was ~20,700,000 days.
        days = (hero.reset_at - self.now) / 86400
        self.assertLess(days, 400, f'reset is {days:,.0f} days away')

    def test_the_card_shows_the_same_span(self):
        snap = cursor(str(int(self.eight_days * 1000)))
        root = u.tk.Tk()
        root.withdraw()
        try:
            card = u.Card(root, 'cursor')
            card.render(snap)
            self.assertAlmostEqual(card._reset_epoch, self.eight_days, delta=1)
            card.refresh_clock(self.now)
            self.assertEqual(card.rows.itemcget('countdown', 'text'), '8일 후')
        finally:
            destroy_root(root)

    def test_a_missing_or_broken_cycle_end_is_not_invented(self):
        for value in (None, '', 'soon', float('nan')):
            with self.subTest(value=repr(value)):
                snap = cursor(value)
                self.assertIsNone(snap.main_limits[0].reset_at)
                self.assertEqual(u.reset_countdown(snap.main_limits[0].reset_at, self.now), '정보 없음')


class CursorCacheMigrationTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.target = self.now + 8 * 86400

    def pre_352_cache(self):
        """What 3.5.0/3.5.1 wrote: Cursor's milliseconds stored as reset_at."""
        millis = self.target * 1000
        return {
            'key': 'cursor', 'title': 'Cursor', 'plan': 'Pro', 'ok': True,
            'hero_percent': 65, 'hero_caption': '', 'stale': False,
            'main_limits': [
                {'quota_id': 'cursor:main:autoPercentUsed', 'source': 'cursor', 'category': 'main',
                 'display_name': 'Cursor Models', 'raw_identifier': 'autoPercentUsed',
                 'window_seconds': None, 'window_label': 'Cursor Models', 'used_percent': 35,
                 'remaining_percent': 65, 'reset_at': millis, 'scope': 'global'}],
            'additional_groups': [], 'billing': [], 'internal': {}}

    def test_a_cached_millisecond_reset_is_migrated_on_restore(self):
        restored = p.snapshot_from_dict(self.pre_352_cache())
        item = restored.main_limits[0]
        self.assertAlmostEqual(item.reset_at, self.target, delta=1)
        self.assertEqual(u.reset_countdown(item.reset_at, self.now, True), '8일 후')

    def test_a_legacy_bar_cache_is_migrated_too(self):
        legacy = {'key': 'cursor', 'title': 'Cursor', 'plan': 'Pro', 'ok': True,
                  'hero_percent': 65, 'hero_caption': '', 'bars': [
                      {'label': 'Cursor Models', 'remaining_percent': 65, 'used_percent': 35,
                       'detail': '', 'usage_scope': json.dumps([self.target * 1000, None])}]}
        restored = p.snapshot_from_dict(legacy)
        self.assertAlmostEqual(restored.main_limits[0].reset_at, self.target, delta=1)

    def test_other_providers_cached_seconds_are_untouched(self):
        cache = {'key': 'chatgpt', 'title': 'GPT', 'plan': 'Plus', 'ok': True,
                 'hero_percent': 80, 'hero_caption': '', 'main_limits': [
                     {'quota_id': 'chatgpt:main:primary_window', 'source': 'chatgpt',
                      'category': 'main', 'display_name': '5시간',
                      'raw_identifier': 'primary_window', 'window_seconds': 18000.0,
                      'window_label': '5시간', 'used_percent': 20, 'remaining_percent': 80,
                      'reset_at': self.target, 'scope': 'global'}]}
        restored = p.snapshot_from_dict(cache)
        self.assertAlmostEqual(restored.main_limits[0].reset_at, self.target, delta=1)

    def test_a_352_cache_round_trips_in_seconds(self):
        snap = cursor(str(int(self.target * 1000)))
        stored = p.snapshot_to_dict(snap)
        self.assertAlmostEqual(stored['main_limits'][0]['reset_at'], self.target, delta=1)
        restored = p.snapshot_from_dict(stored)
        self.assertAlmostEqual(restored.main_limits[0].reset_at, self.target, delta=1)


class CrossProviderResetContractTests(unittest.TestCase):
    """Every provider's reset_at means the same thing: absolute Unix seconds."""

    def test_all_three_providers_agree_on_the_unit(self):
        now = time.time()
        target = now + 8 * 86400
        snaps = [gpt(target),
                 cursor(str(int(target * 1000))),
                 claude('2033-05-18T03:33:20Z', now)]
        for snap in snaps:
            for item in snap.main_limits:
                if item.reset_at is None:
                    continue
                with self.subTest(provider=snap.key, quota=item.raw_identifier):
                    # A plausible seconds-epoch: this decade, not the year 58000.
                    self.assertGreater(item.reset_at, 1_600_000_000)
                    self.assertLess(item.reset_at, 2_500_000_000)
                    span_days = abs(item.reset_at - now) / 86400
                    self.assertLess(span_days, 4000, f'{snap.key} reset is {span_days:,.0f} days out')


class LauncherIconTests(unittest.TestCase):
    """app.ico has to reach the executable, not just sit next to it."""

    def resource_types(self, path):
        data = path.read_bytes()
        pe = struct.unpack_from('<I', data, 0x3C)[0]
        sections = struct.unpack_from('<H', data, pe + 6)[0]
        optional = pe + 24
        magic = struct.unpack_from('<H', data, optional)[0]
        directory = optional + (112 if magic == 0x20b else 96)
        rva, _size = struct.unpack_from('<II', data, directory + 2 * 8)
        table = optional + struct.unpack_from('<H', data, pe + 20)[0]
        offset = None
        for index in range(sections):
            entry = table + index * 40
            virtual = struct.unpack_from('<I', data, entry + 8)[0]
            address, raw_size, raw = struct.unpack_from('<III', data, entry + 12)
            if address <= rva < address + max(raw_size, virtual):
                offset = raw + (rva - address)
        if offset is None:
            return set()
        named, ided = struct.unpack_from('<HH', data, offset + 12)
        names = {1: 'RT_CURSOR', 3: 'RT_ICON', 14: 'RT_GROUP_ICON', 16: 'RT_VERSION',
                 24: 'RT_MANIFEST'}
        found = set()
        for index in range(named + ided):
            value = struct.unpack_from('<I', data, offset + 16 + index * 8)[0]
            if not value & 0x80000000:
                found.add(names.get(value, f'type {value}'))
        return found

    def test_the_build_passes_the_icon_to_the_compiler(self):
        script = (PROJECT / 'build_launcher.ps1').read_text(encoding='utf-8')
        self.assertIn('/win32icon:', script)
        self.assertIn('app.ico', script)
        # An absent icon must stop the build rather than silently produce an
        # executable with the generic .NET icon.
        self.assertIn('Test-Path -LiteralPath $icon', script)

    def test_app_ico_is_a_usable_multi_size_icon(self):
        icon = PROJECT / 'assets' / 'icons' / 'app.ico'
        self.assertTrue(icon.is_file())
        raw = icon.read_bytes()
        reserved, kind, count = struct.unpack_from('<HHH', raw, 0)
        self.assertEqual((reserved, kind), (0, 1))
        sizes = set()
        for index in range(count):
            width, height = raw[6 + index * 16], raw[7 + index * 16]
            sizes.add((width or 256, height or 256))
        self.assertIn((16, 16), sizes)
        self.assertIn((32, 32), sizes)
        self.assertIn((256, 256), sizes)

    def test_the_built_launcher_carries_the_icon_resource(self):
        exe = PROJECT / 'AI Usage.exe'
        if not exe.is_file():
            self.skipTest('AI Usage.exe has not been built in this tree')
        types = self.resource_types(exe)
        self.assertIn('RT_ICON', types, f'launcher has no icon resource: {sorted(types)}')
        self.assertIn('RT_GROUP_ICON', types, f'launcher has no icon group: {sorted(types)}')

    def test_the_shortcut_takes_its_icon_from_the_executable(self):
        script = (PROJECT / 'create_shortcut.ps1').read_text(encoding='utf-8')
        self.assertIn('IconLocation', script)
        self.assertIn('$shortcut.IconLocation = $shortcut.TargetPath', script)


class ServiceIconTests(unittest.TestCase):
    def setUp(self):
        self.root = u.tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        destroy_root(self.root)

    def test_known_providers_resolve_to_a_packaged_asset(self):
        for key in ('chatgpt', 'cursor'):
            with self.subTest(key=key):
                self.assertIsNotNone(u.load_service_icon(key, 1.0))

    def test_an_unknown_provider_is_a_safe_miss(self):
        for key in ('', None, 'nope'):
            with self.subTest(key=key):
                self.assertIsNone(u.load_service_icon(key, 1.0))

    def test_every_mapped_icon_is_present_on_disk(self):
        icons = PROJECT / 'assets' / 'icons'
        for name, _ in u.SERVICE_ICONS.values():
            with self.subTest(asset=name):
                self.assertTrue((icons / f'{name}.png').is_file())

    def visible_height(self, key, scale):
        name, height = u.SERVICE_ICONS[key]
        w, h, rows = raster.read_png_rgba(u.service_icon_png(name, u.px(height, scale, 1)))
        lit = [y for y in range(h) if any(rows[y][x*4+3] > 64 for x in range(w))]
        return lit[-1] - lit[0] + 1

    def test_marks_look_the_same_size_at_every_scale(self):
        # The Blossom file carries clear space; the cube has none. Their
        # visible marks, not their files, are what must match.
        for scale in (0.75, 1.0, 1.15, 1.5):
            with self.subTest(scale=scale):
                gpt, cursor = self.visible_height('chatgpt', scale), self.visible_height('cursor', scale)
                self.assertLessEqual(abs(gpt - cursor), 2)
                self.assertAlmostEqual(gpt, 16 * scale, delta=2)

    def test_collapse_chevron_points_its_way_with_soft_edges(self):
        down = raster.read_png_rgba(raster.chevron_png(11, True, u.ICON, 1.6))
        right = raster.read_png_rgba(raster.chevron_png(11, False, u.ICON, 1.6))
        self.assertEqual(down[:2], (11, 11))
        self.assertNotEqual(down[2], right[2])
        for _, _, rows in (down, right):
            alphas = {row[x*4+3] for row in rows for x in range(11)}
            self.assertIn(255, alphas)
            self.assertTrue(any(0 < a < 255 for a in alphas))
            # Drawn in the header icons' colour, not the dim meta grey.
            lit = next(row[x*4:x*4+3] for row in rows for x in range(11) if row[x*4+3] == 255)
            self.assertEqual('#%02X%02X%02X' % tuple(lit), u.ICON.upper())

    def test_shrinking_keeps_soft_edges(self):
        # Area averaging leaves partial alpha on the outline; nearest-pixel
        # picking, which the old Tk zoom/subsample did, leaves none.
        name, height = u.SERVICE_ICONS['chatgpt']
        w, h, rows = raster.read_png_rgba(u.service_icon_png(name, height))
        alphas = {rows[y][x*4+3] for y in range(h) for x in range(w)}
        self.assertTrue(any(0 < a < 255 for a in alphas))

    def test_the_whole_icon_folder_is_packaged(self):
        # publish_update.ps1 copies the directory, so a new asset needs no
        # extra entry in the copy list.
        script = (PROJECT / 'publish_update.ps1').read_text(encoding='utf-8')
        self.assertIn("Join-Path $project 'assets\\icons'", script)


if __name__ == '__main__':
    unittest.main()
