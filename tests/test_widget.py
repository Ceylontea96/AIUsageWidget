import json
import math
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import providers as p
import usage_widget as u
import widget_raster as raster


def codex(primary=10, weekly=20, reached=False):
    body={'rate_limit': {
        'primary_window': {'used_percent':primary,'reset_after_seconds':3600,'limit_window_seconds':18000},
        'secondary_window': {'used_percent':weekly,'reset_after_seconds':86400,'limit_window_seconds':604800},
        'limit_reached':reached,
    }}
    return p.chatgpt_snapshot(body)


def codex_rate(rate):
    body={'rate_limit':rate}
    return p.chatgpt_snapshot(body)


class WidgetTests(unittest.TestCase):
    def test_scale_helpers(self):
        self.assertEqual(u.clamp_scale(None), 1.0)
        self.assertEqual(u.clamp_scale('nope'), 1.0)
        self.assertEqual(u.clamp_scale(0.5), 0.75)
        self.assertEqual(u.clamp_scale(2), 1.5)
        self.assertEqual(u.clamp_scale(1.15), 1.15)
        self.assertEqual(u.clamp_scale(float('nan')), 1.0)
        self.assertEqual(u.clamp_scale(float('inf')), 1.0)
        self.assertEqual(u.px(360, 1.0), 360)
        self.assertEqual(u.px(360, 1.15), 414)
        self.assertEqual(u.px(8, 0.85), 7)
        self.assertEqual(u.step_scale(1.0, 1), 1.15)
        self.assertEqual(u.step_scale(1.15, 1), 1.3)
        self.assertEqual(u.step_scale(1.0, -1), 0.85)
        self.assertEqual(u.step_scale(0.85, -1), 0.75)
        self.assertEqual(u.step_scale(1.45, 1), 1.5)
        self.assertEqual(u.scaled_font(('Segoe UI Semibold', -13), 1.0), ('Segoe UI Semibold', -13))
        self.assertEqual(u.scaled_font(('Segoe UI Semibold', -13), 1.3), ('Segoe UI Semibold', -17))
        self.assertEqual(u.Metrics(1.15).window_w, u.px(u.WINDOW_W,1.15))
        self.assertEqual(u.Metrics(1.15).header_h, u.px(u.HEADER_H,1.15))

    def test_weekly_exhaustion(self):
        s=codex(10,100,True)
        self.assertEqual(s.hero_percent,90)
        self.assertTrue(s.blocked)
        self.assertEqual(s.hero_caption,'주간 소진')
        self.assertEqual(u.chip_style('chatgpt', s), (u.CHIP_DANGER, u.CHIP_FG))

    def test_short_exhaustion(self):
        self.assertEqual(codex(100,20).hero_caption,'5시간 소진')

    def test_unknown_restriction(self):
        s=codex(10,20,True)
        self.assertTrue(s.blocked)
        self.assertIn('상세 확인',s.hero_caption)
        self.assertEqual(u.chip_style('chatgpt', s), (u.CHIP_DANGER, u.CHIP_FG))

    def test_five_hour_window_is_preferred_for_hero(self):
        self.assertEqual(codex(10,80).hero_percent,90)

    def test_weekly_only_primary_is_not_mislabeled_as_five_hours(self):
        s=codex_rate({'primary_window': {
            'used_percent':0,'reset_after_seconds':604000,'limit_window_seconds':604800,
        }})
        self.assertEqual([bar.label for bar in s.bars], ['주간'])
        self.assertEqual(s.hero_percent,100)
        self.assertEqual(s.hero_caption,'주간 기준 잔여')
        self.assertEqual(u.hero_index(s),0)

    def test_reversed_window_positions_are_classified_by_duration(self):
        s=codex_rate({
            'primary_window': {'used_percent':30,'reset_at':2000000000,'limit_window_seconds':604800},
            'secondary_window': {'used_percent':20,'reset_at':1900000000,'limit_window_seconds':18000},
        })
        self.assertEqual([bar.label for bar in s.bars], ['5시간','주간'])
        self.assertEqual([bar.remaining_percent for bar in s.bars], [80,70])
        self.assertEqual(json.loads(s.bars[0].usage_scope)[0],1900000000)
        self.assertEqual(json.loads(s.bars[1].usage_scope)[0],2000000000)

    def test_unknown_window_uses_duration_or_safe_generic_label(self):
        one_day=codex_rate({'primary_window': {
            'used_percent':25,'reset_after_seconds':3600,'limit_window_seconds':86400,
        }})
        self.assertEqual(one_day.bars[0].label,'1일')
        self.assertEqual(one_day.hero_caption,'1일 기준 잔여')
        unknown=codex_rate({'primary_window': {'used_percent':25,'reset_after_seconds':3600}})
        self.assertEqual(unknown.bars[0].label,'기간 미상')
        self.assertNotIn('5시간',unknown.hero_caption)

    def test_duration_classification_uses_safe_tolerance(self):
        self.assertEqual(p._duration_label(17940),'5시간')
        self.assertEqual(p._duration_label(604000),'주간')
        self.assertEqual(p._duration_label(30*86400),'30일')
        self.assertEqual(p._duration_label(4*3600),'4시간')
        self.assertEqual(p._duration_label(6*86400),'6일')
        self.assertEqual(p._duration_label(8*86400),'8일')

    def test_weekly_low_quota_still_controls_warning(self):
        # The risk channel follows the weekly window even though the hero does not.
        self.assertEqual(u.service_state(codex(10,95)), 'danger')
        self.assertEqual(u.service_state(codex(10,75)), 'warn')
        self.assertEqual(u.representative_state(codex(10,95)), 'ok')

    def test_missing_data_does_not_claim_success(self):
        with self.assertRaises(RuntimeError):codex(None,None)

    def test_nonfinite_values(self):
        for v in ['nan','inf',float('-inf'),False]:self.assertIsNone(p.to_float(v))

    def test_malformed_cache_lists(self):
        for v in ([None,2,'wrong'],{},'wrong'):
            self.assertEqual(p.snapshot_from_dict({'bars':v,'info_rows':v}).bars,[])

    def test_absolute_reset(self):
        item=p._quota_item_from_window(quota_id='q',source='chatgpt',category='main',
            display_name='test',raw_identifier='w',window={'used_percent':10,'reset_after_seconds':3600},
            scope='global')
        self.assertIsNotNone(item.reset_at)
        self.assertIn('초기화',p.bar_from_quota_item(item).reset_text+' 초기화')
        # A reset is shown as an absolute stamp, never as a relative countdown.
        self.assertNotIn('후',p.fmt_local(item.reset_at,'reset'))

    def test_reset_stamp_and_bar_colors(self):
        self.assertEqual(u.reset_stamp('9월 10일 19:52'),'19:52 리셋')
        self.assertEqual(u.reset_stamp(''),'')
        self.assertEqual(u.chip_fill_width(100,47),47)
        self.assertEqual(u.chip_fill_width(200,11),22)
        self.assertEqual(u.chip_fill_width(100,0),0)
        self.assertEqual(u.chip_fill_width(100,None),0)
        self.assertFalse(u.should_setup({'version':3,'enabled':{'chatgpt':True}},preview=False))
        self.assertTrue(u.should_setup({},preview=False))
        self.assertFalse(u.should_setup({},preview=True))
        self.assertEqual(u.default_enabled({},present={'chatgpt':False,'cursor':True}),{'chatgpt':False,'cursor':True,'claude':False})
        self.assertEqual(
            u.default_enabled({'version':3,'enabled':{'chatgpt':True,'cursor':False}},present={'chatgpt':False,'cursor':True}),
            {'chatgpt':True,'cursor':False,'claude':False},
        )
        work, monitor = (0, 0, 1920, 1040), (0, 0, 1920, 1080)
        self.assertEqual(u.clamp_position(100, 2000, 360, 44, work, monitor), (100, 1036))
        self.assertEqual(u.clamp_position(100, 990, 360, 44, work, monitor), (100, 996))
        self.assertEqual(u.clamp_position(-80, -20, 360, 44, work, monitor), (8, 8))
        w,h,rows=raster.progress_bar_rgba(100,8,4,12,'#262A36','#F26D6D','#1A1D26')
        def px(x,y):
            return tuple(rows[y][x*4:x*4+4])
        mid_top, cap_top = px(50,0), px(11,0)
        self.assertLessEqual(cap_top[3], mid_top[3]+2)
        self.assertLessEqual(abs(cap_top[0]-mid_top[0]), 12)
        fill=px(4,4); rest=px(80,4)
        self.assertGreater(fill[0], 180)
        self.assertLess(rest[0], 80)
        self.assertGreater(fill[3], 250)
        self.assertGreater(rest[3], 250)
        sw,sh,srows=raster.padded_stadium_rgba(3,40,1.5,'#F26D6D','#1A1D26',pad=1,samples=4)
        self.assertEqual((sw,sh),(5,42))
        bg, tip, mid = srows[0][0], srows[1][8], srows[21][8]
        self.assertLess(srows[0][0], 40)
        self.assertGreater(mid, 180)
        self.assertGreater(tip, 180)
        self.assertLess(srows[1][0], tip)
        stale=codex();stale.stale=True
        self.assertEqual(u.representative_state(stale),'stale')
        self.assertEqual(u.chip_style('chatgpt', stale), (u.CHIP_STALE, u.CHIP_FG))

    def test_day_format(self):
        self.assertEqual(u.reset_countdown(123*3600, now=0, monthly=True), '5일 후')
        self.assertEqual(u.reset_countdown(123*3600, now=0), '123시간 0분')

    def test_backoff_and_recovery(self):
        s=codex()
        self.assertEqual([u.next_interval(s,i) for i in (1,2,3,6,20)],[30,60,120,900,900])
        self.assertEqual(u.next_interval(s,0),30)

    def test_exhausted_slow_poll(self):
        self.assertEqual(u.next_interval(codex(100,20)),300)

    def test_low_remaining_polls_faster(self):
        self.assertEqual(u.next_interval(codex(70,20)),20)
        self.assertEqual(u.next_interval(codex(95,20)),20)
        self.assertEqual(u.next_interval(codex(10,80)),20)

    def test_install_root_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'widget'
            root.mkdir()
            (root / 'usage_widget.py').write_text('x', encoding='utf-8')
            (root / 'setup_and_run.ps1').write_text('x', encoding='utf-8')
            store = Path(directory) / 'install.json'
            with patch.object(u, 'INSTALL_PATH', store):
                self.assertTrue(u.is_widget_root(root))
                self.assertFalse(u.is_widget_root(directory))
                self.assertEqual(u.save_install_root(root), root.resolve())
                self.assertEqual(u.read_json(store)['root'], str(root.resolve()))
                u.save_install_root(root, shortcut_asked=True)
                self.assertTrue(u.read_json(store)['shortcut_asked'])

    def test_create_desktop_shortcut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'widget'
            desktop = Path(directory) / 'Desktop'
            root.mkdir()
            desktop.mkdir()
            (root / 'usage_widget.py').write_text('x', encoding='utf-8')
            (root / 'setup_and_run.ps1').write_text('x', encoding='utf-8')
            (root / u.LAUNCHER_EXE).write_bytes(b'MZ')
            store = Path(directory) / 'install.json'
            with patch.object(u, 'INSTALL_PATH', store):
                path = u.create_desktop_shortcut(root, desktop)
            self.assertTrue(path.is_file())
            self.assertEqual(path.name, u.SHORTCUT_NAME)
            self.assertTrue(u.read_json(store).get('shortcut_asked'))

    def test_failed_provider_keeps_last_good(self):
        w=u.UsageWidget.__new__(u.UsageWidget)
        w.failures={'chatgpt':0};w.snapshots={'chatgpt':codex()};w.due={};w.preview=True;w.render=lambda k:None
        w.usage_until={};w.request_started={};w.cards={};w.mini_values={}
        w.codex_activity=u.CodexActivityMonitor()
        w.accept('chatgpt',p.error_snapshot('chatgpt','Codex','offline',''))
        self.assertTrue(w.snapshots['chatgpt'].stale)
        self.assertEqual(w.failures['chatgpt'],1)
        w.accept('chatgpt',codex())
        self.assertFalse(w.snapshots['chatgpt'].stale)
        self.assertEqual(w.failures['chatgpt'],0)

    def test_gpt_never_reads_codex_tokens(self):
        # GPT usage comes from Codex's own app-server; the widget holds no
        # reader for Codex's login file at all.
        self.assertFalse(hasattr(p,'ChatGptAuth'))
        source=Path(p.__file__).read_text(encoding='utf-8')
        self.assertNotIn('auth.json',source)
        self.assertNotIn('wham/usage',source)

    def test_cursor_auth_is_read_only(self):
        with patch.object(p,'jwt_exp',return_value=0),patch.object(p,'http_json') as http:
            auth=p.CursorAuth();auth.access_token='fake'
            with self.assertRaises(RuntimeError):auth.ensure_fresh()
            http.assert_not_called()

    def test_cache_round_trip(self):
        s=codex(100,20)
        out=p.snapshot_from_dict(p.snapshot_to_dict(s))
        self.assertTrue(out.stale)
        self.assertTrue(out.blocked)
        self.assertEqual(out.hero_percent,0)

    def test_corrupt_json(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'cache.json';path.write_text('{broken')
            self.assertEqual(u.read_json(path),{})
            u.save_json(path,{'ok':True});self.assertEqual(u.read_json(path),{'ok':True})

    def test_http_validates_response(self):
        class Response:
            status=200
            def __init__(self,body):self.parts=[body,b'']
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read1(self,size):return self.parts.pop(0)
        with patch.object(p._OPENER,'open',return_value=Response(b'{"value":1}')):
            self.assertEqual(p.http_json('GET','https://example.invalid',{}),(200,{'value':1}))
        with patch.object(p._OPENER,'open',return_value=Response(b'[]')):
            with self.assertRaises(RuntimeError):p.http_json('GET','https://example.invalid',{})

    def test_record_crash_writes_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(u, 'APP_DIR', Path(directory)):
                try:
                    raise RuntimeError('boom')
                except RuntimeError:
                    text = u.record_crash()
                log = (Path(directory) / 'error.log').read_text(encoding='utf-8')
                self.assertIn('boom', text)
                self.assertIn('boom', log)
                self.assertIn('RuntimeError', log)

    def test_lock_helpers_and_launch_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'widget.instance').write_text(f'{os.getpid()}\n12345\n', encoding='ascii')
            with patch.object(u, 'APP_DIR', root):
                pid, hwnd = u.read_lock()
                self.assertEqual(pid, os.getpid())
                self.assertEqual(hwnd, 12345)
                self.assertTrue(u.process_alive(os.getpid()))
                self.assertFalse(u.process_alive(0))
                u.log_launch('hello')
                self.assertIn('hello', (root / 'launch.log').read_text(encoding='utf-8'))
                (root / 'widget.lock').write_text('0\n', encoding='ascii')
                (root / 'widget.instance').write_text('0\n0\n', encoding='ascii')
                with patch.object(u, 'activate_existing', return_value=False), \
                     patch.object(u.time, 'sleep'), patch.object(u, 'terminate_pid') as kill:
                    self.assertEqual(u.recover_busy_lock(), 'cleared')
                    kill.assert_not_called()
                self.assertFalse((root / 'widget.lock').exists())
                self.assertFalse((root / 'widget.instance').exists())

    def test_launch_log_keeps_one_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'launch.log').write_text('x' * (u.LOG_LIMIT + 1), encoding='utf-8')
            with patch.object(u, 'APP_DIR', root):
                u.log_launch('fresh')
            self.assertEqual((root / 'launch.log.1').stat().st_size, u.LOG_LIMIT + 1)
            self.assertIn('fresh', (root / 'launch.log').read_text(encoding='utf-8'))
            self.assertLess((root / 'launch.log').stat().st_size, 100)

    def test_callback_errors_are_written_once_a_minute_with_a_repeat_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime-error.log'
            now = [0.0]
            errors = u.CallbackErrors(path, clock=lambda: now[0])

            def fail():
                try:
                    raise ValueError('bad value')
                except ValueError as exc:
                    return errors.report(type(exc), exc, exc.__traceback__)

            self.assertTrue(fail())
            self.assertFalse(fail())
            self.assertFalse(fail())
            now[0] = u.CALLBACK_ERROR_REPEAT + 1
            self.assertTrue(fail())
            text = path.read_text(encoding='utf-8')
            self.assertEqual(text.count('ValueError: bad value'), 2)
            self.assertIn('(+2 repeats)', text)

    def test_startup_entry_starts_the_launcher_not_a_fixed_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'widget'
            root.mkdir()
            (root / 'start_usage_widget.vbs').write_text("' launcher", encoding='utf-8')
            entry = Path(directory) / 'Startup' / 'AIUsageWidget.vbs'
            with patch.object(u, 'startup_path', return_value=entry):
                u.set_startup(True, root)
                text = entry.read_text(encoding='utf-16')
                self.assertIn(f'wscript.exe ""{root.resolve()}\\start_usage_widget.vbs""', text)
                self.assertNotIn('pythonw', text)
                self.assertNotIn('\r\r', entry.read_bytes().decode('utf-16'))
                self.assertFalse(u.sync_startup(root))
                u.set_startup(False)
                self.assertFalse(entry.exists())

    def test_old_startup_entry_is_moved_to_the_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'widget'
            root.mkdir()
            (root / 'start_usage_widget.vbs').write_text("' launcher", encoding='utf-8')
            entry = Path(directory) / 'AIUsageWidget.vbs'
            entry.write_text('sh.Run """C:\\Python312\\pythonw.exe"" ""usage_widget.py""", 0, False\n', encoding='utf-16')
            with patch.object(u, 'startup_path', return_value=entry):
                self.assertTrue(u.sync_startup(root))
                self.assertIn('start_usage_widget.vbs', entry.read_text(encoding='utf-16'))
                # No entry means the user never asked for one: none is made.
                entry.unlink()
                self.assertFalse(u.sync_startup(root))
                self.assertFalse(entry.exists())

    def test_activate_existing_uses_instance_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'widget.instance').write_text(f'{os.getpid()}\n4242\n', encoding='ascii')
            with patch.object(u, 'APP_DIR', root), patch.object(u, 'show_window', return_value=True) as show:
                self.assertTrue(u.activate_existing())
                show.assert_called_with(4242)

    def test_recover_busy_lock_activates_without_killing(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(u, 'APP_DIR', Path(directory)), patch.object(u, 'activate_existing', return_value=True), patch.object(u, 'terminate_pid') as kill:
                self.assertEqual(u.recover_busy_lock(), 'activated')
                kill.assert_not_called()

    def test_oversize_response_rejected(self):
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read1(self,size):return b'x'*65536
        with patch.object(p._OPENER,'open',return_value=Response()):
            with self.assertRaises(RuntimeError):p.http_json('GET','https://example.invalid',{})


if __name__=='__main__':unittest.main(verbosity=2)
