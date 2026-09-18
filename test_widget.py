import json
import math
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import providers as p
import usage_widget as u


class FakeAuth:
    def load(self): pass
    def ensure_fresh(self): pass
    def access_token(self): return 'fake'
    def account_id(self): return ''


def codex(primary=10, weekly=20, reached=False):
    body={'rate_limit': {
        'primary_window': {'used_percent':primary,'limit_window_seconds':18000,'reset_after_seconds':3600},
        'secondary_window': {'used_percent':weekly,'limit_window_seconds':604800,'reset_after_seconds':86400},
        'limit_reached':reached,
    }}
    with patch.object(p,'ChatGptAuth',FakeAuth),patch.object(p,'http_json',return_value=(200,body)):
        return p.fetch_chatgpt()


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
        self.assertEqual(u.scaled_font(u.FONT_TITLE, 1.0), u.FONT_TITLE)
        self.assertEqual(u.scaled_font(u.FONT_TITLE, 1.3), (u.FONT_TITLE[0], -17))
        self.assertEqual(u.Metrics(1.15).window_w, 437)
        self.assertEqual(u.Metrics(1.15).header_h, 44)

    def test_pretendard_faces_when_bundled(self):
        files = [u.FONT_DIR / name for name in u.PRETENDARD_FILES]
        if not all(path.is_file() for path in files):
            self.skipTest('Pretendard files are not bundled')
        self.assertTrue(u.register_bundled_fonts())
        for spec in (u.FONT_TITLE, u.FONT_HERO, u.FONT_SERVICE, u.FONT_CHIP, u.FONT_VALUE, u.FONT_PILL):
            self.assertEqual(spec[0], 'Pretendard SemiBold')
        for spec in (u.FONT_SUB, u.FONT_ROW, u.FONT_BADGE):
            self.assertEqual(spec[0], 'Pretendard Medium')
        for spec in (u.FONT_META, u.FONT_FOOT):
            self.assertEqual(spec[0], 'Pretendard')

    def test_weekly_exhaustion(self):
        s=codex(10,100,True)
        self.assertEqual(s.hero_percent,90)
        self.assertTrue(s.blocked)
        self.assertEqual(s.hero_caption,'5시간 기준 잔여')
        self.assertEqual(u.color_for(s),u.CODEX)

    def test_short_exhaustion(self):
        self.assertEqual(codex(100,20).hero_caption,'5시간 소진')

    def test_unknown_restriction(self):
        s=codex(10,20,True)
        self.assertTrue(s.blocked)
        self.assertEqual(s.hero_caption,'5시간 기준 잔여')
        self.assertEqual(u.color_for(s),u.CODEX)

    def test_tightest_window(self):
        self.assertEqual(codex(10,80).hero_percent,90)

    def test_missing_data_does_not_claim_success(self):
        with self.assertRaises(RuntimeError):codex(None,None)

    def test_nonfinite_values(self):
        for v in ['nan','inf',float('-inf'),False]:self.assertIsNone(p.to_float(v))

    def test_malformed_cache_lists(self):
        for v in ([None,2,'wrong'],{},'wrong'):
            self.assertEqual(p.snapshot_from_dict({'bars':v,'info_rows':v}).bars,[])

    def test_absolute_reset(self):
        self.assertIn('초기화',p._window_bar('test',{'used_percent':10,'reset_after_seconds':3600}).reset_text+' 초기화')
        self.assertNotIn('후',p._window_bar('test',{'reset_after_seconds':3600}).reset_text)

    def test_reset_stamp_and_bar_colors(self):
        self.assertEqual(u.reset_stamp('9월 10일 19:52'),'19:52 리셋')
        self.assertEqual(u.reset_stamp(''),'')
        self.assertEqual(u.dated_reset_stamp('9월 17일 08:30'),'9월 17일 08:30 리셋')
        self.assertEqual(u.dated_reset_stamp(''),'')
        self.assertEqual(u.cursor_reset('9월 14일 09:00'),'9월 14일 09:00 초기화')
        self.assertEqual(u.cursor_reset('9월 14일 09:00 초기화'),'9월 14일 09:00 초기화')
        self.assertEqual(u.cursor_reset(''),'')
        self.assertEqual(u.bar_color('chatgpt',46,False),u.CODEX)
        self.assertEqual(u.bar_color('chatgpt',18,False),u.WARN)
        self.assertEqual(u.bar_color('chatgpt',8,False),u.DANGER)
        self.assertEqual(u.bar_color('chatgpt',8,True),u.CODEX)
        self.assertTrue(u.should_tween(80,50))
        self.assertTrue(u.should_tween(50,80))
        self.assertFalse(u.should_tween(50,50))
        self.assertEqual(u.follow_emphasis(0, True, 0), 0)
        self.assertEqual(u.follow_emphasis(0, True, 0.4), 1)
        self.assertAlmostEqual(u.follow_emphasis(0, True, 0.2), 0.5)
        self.assertEqual(u.follow_emphasis(1, True, 10), 1)
        self.assertEqual(u.follow_emphasis(1, False, 0.85), 0)
        self.assertAlmostEqual(u.next_fast_due(100, 100.8), 102)
        self.assertEqual(u.next_fast_due(100, 103), 103)
        self.assertAlmostEqual(u.follow_bar(0, 100, 0.1), 55.0671035883)
        self.assertEqual(u.follow_bar(50, 50, 1), 50)
        self.assertEqual(u.follow_bar(50, 49, 0.6), 49)
        self.assertAlmostEqual(u.follow_bar(u.follow_bar(0, 100, 0.2), 100, 0.3), u.follow_bar(0, 100, 0.5))
        self.assertEqual(u.chip_fill_width(100,47),47)
        self.assertEqual(u.chip_fill_width(200,11),22)
        self.assertEqual(u.chip_fill_width(100,0),0)
        self.assertEqual(u.chip_fill_width(100,None),0)
        self.assertFalse(u.should_setup({'version':3,'enabled':{'chatgpt':True}},preview=False))
        self.assertTrue(u.should_setup({},preview=False))
        self.assertFalse(u.should_setup({},preview=True))
        self.assertEqual(u.default_enabled({},present={'chatgpt':False,'cursor':True}),{'chatgpt':False,'cursor':True})
        self.assertEqual(
            u.default_enabled({'version':3,'enabled':{'chatgpt':True,'cursor':False}},present={'chatgpt':False,'cursor':True}),
            {'chatgpt':True,'cursor':False},
        )
        work, monitor = (0, 0, 1920, 1040), (0, 0, 1920, 1080)
        with patch.object(u, 'work_area', return_value=work):
            self.assertEqual(u.center_box(400, 200, 10, 10), (760, 420))
        with patch.object(u, 'work_area', return_value=(1920, 0, 3840, 1080)):
            self.assertEqual(u.center_box(200, 100, 2000, 10), (2780, 490))
        self.assertEqual(u.clamp_position(100, 2000, 360, 44, work, monitor), (100, 1036))
        self.assertEqual(u.clamp_position(100, 990, 360, 44, work, monitor), (100, 996))
        self.assertEqual(u.clamp_position(-80, -20, 360, 44, work, monitor), (8, 8))
        w,h,rows=u.progress_bar_rgba(100,8,4,12,'#262A36','#F26D6D','#1A1D26')
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
        sw,sh,srows=u.padded_stadium_rgba(3,40,1.5,'#F26D6D','#1A1D26',pad=1,samples=4)
        self.assertEqual((sw,sh),(5,42))
        bg, tip, mid = srows[0][0], srows[1][8], srows[21][8]
        self.assertLess(srows[0][0], 40)
        self.assertGreater(mid, 180)
        self.assertGreater(tip, 180)
        self.assertLess(srows[1][0], tip)
        stale=codex();stale.stale=True
        self.assertEqual(u.visual_state(stale),'stale')
        self.assertEqual(u.color_for(stale),u.STALE_HERO)

    def test_day_format(self):
        self.assertEqual(p.fmt_eta(123*3600),'5일 3시간 후')

    def test_backoff_and_recovery(self):
        s=codex()
        self.assertEqual([u.next_interval(s,i) for i in (1,2,3,6,20)],[30,60,120,900,900])
        self.assertEqual(u.next_interval(s,0),30)

    def test_exhausted_slow_poll(self):
        self.assertEqual(u.next_interval(codex(100,20)),300)

    def test_low_remaining_polls_faster(self):
        self.assertEqual(u.next_interval(codex(70,20)),20)
        self.assertEqual(u.next_interval(codex(95,20)),20)

    def test_usage_drop_polls_faster_then_cools_down(self):
        first, second = codex(10,20), codex(20,20)
        self.assertTrue(u.usage_dropped(first, second))
        self.assertFalse(u.usage_dropped(second, second))
        self.assertFalse(u.usage_dropped(second, first))
        self.assertEqual(u.next_interval(second, active=True), 2)
        self.assertEqual(u.next_interval(second, active=False), 30)
        self.assertEqual(u.next_interval(codex(100,20), active=True), 300)
        w=u.UsageWidget.__new__(u.UsageWidget)
        w.failures={'chatgpt':0,'cursor':0};w.snapshots={};w.due={};w.usage_until={};w.preview=True;w.render=lambda k:None
        w.request_started={'chatgpt':float('-inf'),'cursor':float('-inf')}
        w.accept('chatgpt', first)
        self.assertLessEqual(w.usage_until.get('chatgpt', 0), time.monotonic())
        w.accept('chatgpt', second)
        self.assertFalse(w.usage_until.get('chatgpt', 0))
        self.assertGreater(w.due['chatgpt'] - time.monotonic(), 29)
        w.codex_activity = u.CodexActivityMonitor()
        started = time.monotonic()
        w.request_started['chatgpt'] = started
        w.codex_activity.last_activity_time = started
        w.accept('chatgpt', second)
        self.assertAlmostEqual(w.due['chatgpt'], started + 2, delta=0.1)
        w.codex_activity.last_activity_time -= 13
        w.accept('chatgpt', second)
        self.assertGreater(w.due['chatgpt'] - time.monotonic(), 29)
        cursor_first, cursor_second = replace(first, key='cursor'), replace(second, key='cursor')
        w.request_started['cursor'] = started
        w.accept('cursor', cursor_first)
        w.accept('cursor', cursor_second)
        self.assertGreater(w.usage_until['cursor'], time.monotonic())
        self.assertAlmostEqual(w.due['cursor'], started + 2, delta=0.1)

    def test_cursor_transcript_fast_without_usage_change(self):
        snap = replace(codex(), key='cursor')
        w=u.UsageWidget.__new__(u.UsageWidget)
        w.failures={'cursor':0};w.snapshots={};w.due={};w.usage_until={};w.preview=True;w.render=lambda k:None
        w.request_started={'cursor':100.0}
        w.cursor_activity=u.CursorActivityMonitor()
        w.cursor_activity.last_activity_time=100.0
        with patch.object(time,'monotonic',return_value=100.5):
            w.accept('cursor', snap)
        self.assertEqual(w.due['cursor'], 102.0)
        self.assertLessEqual(w.usage_until.get('cursor', 0), 100.5)

    def test_fast_poll_is_start_to_start_and_skips_running_worker(self):
        snap = codex()
        w=u.UsageWidget.__new__(u.UsageWidget)
        w.failures={'chatgpt':0};w.snapshots={};w.due={};w.usage_until={};w.preview=True;w.render=lambda k:None
        w.request_started={'chatgpt':100.0}
        w.codex_activity=u.CodexActivityMonitor()
        w.codex_activity.last_activity_time=100.0
        with patch.object(time,'monotonic',return_value=100.8):
            w.accept('chatgpt', snap)
        self.assertEqual(w.due['chatgpt'], 102.0)
        with patch.object(time,'monotonic',return_value=103.0):
            w.accept('chatgpt', snap)
        self.assertEqual(w.due['chatgpt'], 103.0)
        w.runner=Mock()
        w.runner.start.return_value=False
        w.poll_pending={'chatgpt':False}
        w.codex_last_request=100.0
        w.start_job('chatgpt')
        self.assertEqual(w.request_started['chatgpt'], 100.0)
        w.runner.start.return_value=True
        with patch.object(time,'monotonic',return_value=104.0):
            w.start_job('chatgpt')
        self.assertEqual(w.request_started['chatgpt'], 104.0)
        self.assertEqual(w.codex_last_request, 104.0)

    def test_activity_ui_follows_codex_fast_state(self):
        w=u.UsageWidget.__new__(u.UsageWidget)
        w.preview=False
        w.enabled={'chatgpt': Mock(get=lambda: True), 'cursor': Mock(get=lambda: False)}
        gpt_card, gpt_chip = Mock(), Mock()
        w.cards={'chatgpt': gpt_card, 'cursor': Mock()}
        w.mini_values={'chatgpt': gpt_chip, 'cursor': Mock()}
        w.codex_activity=u.CodexActivityMonitor()
        w.codex_activity.last_activity_time=time.monotonic()
        w.usage_until={}
        w._ui_active={}
        with self.assertLogs('ai_usage.activity', level='DEBUG') as logged:
            w._sync_activity_ui()
        gpt_card.set_activity.assert_called_with(True)
        gpt_chip.set_activity.assert_called_with(True)
        self.assertTrue(any('GPT bar ACTIVE' in line for line in logged.output))
        self.assertTrue(any('GPT shimmer ACTIVE' in line for line in logged.output))
        w.codex_activity.last_activity_time=float('-inf')
        with self.assertLogs('ai_usage.activity', level='DEBUG') as logged:
            w._sync_activity_ui()
        gpt_card.set_activity.assert_called_with(False)
        self.assertTrue(any('GPT bar NORMAL' in line for line in logged.output))
        self.assertTrue(any('GPT shimmer STOP' in line for line in logged.output))

    def test_activity_ui_follows_cursor_fast_state(self):
        w=u.UsageWidget.__new__(u.UsageWidget)
        w.preview=False
        w.enabled={'chatgpt': Mock(get=lambda: False), 'cursor': Mock(get=lambda: True)}
        card, chip = Mock(), Mock()
        w.cards={'chatgpt': Mock(), 'cursor': card}
        w.mini_values={'chatgpt': Mock(), 'cursor': chip}
        w.codex_activity=u.CodexActivityMonitor()
        w.cursor_activity=Mock()
        w.cursor_activity.visual_active.side_effect=[True,False]
        w.usage_until={}
        w._ui_active={}
        with self.assertLogs('ai_usage.activity', level='DEBUG') as logged:
            w._sync_activity_ui()
        card.set_activity.assert_called_with(True)
        chip.set_activity.assert_called_with(True)
        self.assertTrue(any('Cursor bar ACTIVE' in line for line in logged.output))
        with self.assertLogs('ai_usage.activity', level='DEBUG') as logged:
            w._sync_activity_ui()
        card.set_activity.assert_called_with(False)
        self.assertTrue(any('Cursor bar NORMAL' in line for line in logged.output))

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
                self.assertEqual(u.read_install_root(), root.resolve())
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
        w.accept('chatgpt',p.error_snapshot('chatgpt','Codex','offline',''))
        self.assertTrue(w.snapshots['chatgpt'].stale)
        self.assertEqual(w.failures['chatgpt'],1)
        w.accept('chatgpt',codex())
        self.assertFalse(w.snapshots['chatgpt'].stale)
        self.assertEqual(w.failures['chatgpt'],0)

    def test_auth_is_read_only(self):
        with patch.object(p,'jwt_exp',return_value=0),patch.object(p,'http_json') as http:
            auth=p.ChatGptAuth();auth.data={'tokens':{'access_token':'fake','refresh_token':'fake'}}
            with self.assertRaises(RuntimeError):auth.ensure_fresh()
            http.assert_not_called()
            self.assertEqual(auth.data['tokens']['access_token'],'fake')

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
                self.assertTrue(u.clear_stale_lock())
                self.assertFalse((root / 'widget.lock').exists())
                self.assertFalse((root / 'widget.instance').exists())

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
