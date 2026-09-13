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


class FakeAuth:
    def load(self): pass
    def ensure_fresh(self): pass
    def access_token(self): return 'fake'
    def account_id(self): return ''


def codex(primary=10, weekly=20, reached=False):
    body={'rate_limit': {'primary_window': {'used_percent':primary,'reset_after_seconds':3600}, 'secondary_window': {'used_percent':weekly,'reset_after_seconds':86400}, 'limit_reached':reached}}
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
        self.assertEqual(u.scaled_font(('Segoe UI Semibold', -13), 1.0), ('Segoe UI Semibold', -13))
        self.assertEqual(u.scaled_font(('Segoe UI Semibold', -13), 1.3), ('Segoe UI Semibold', -17))
        self.assertEqual(u.Metrics(1.15).window_w, 414)
        self.assertEqual(u.Metrics(1.15).header_h, 46)

    def test_weekly_exhaustion(self):
        s=codex(10,100,True)
        self.assertEqual(s.hero_percent,0)
        self.assertTrue(s.blocked)
        self.assertEqual(s.hero_caption,'주간 소진')
        self.assertEqual(u.color_for(s),u.RED)

    def test_short_exhaustion(self):
        self.assertEqual(codex(100,20).hero_caption,'5시간 소진')

    def test_unknown_restriction(self):
        s=codex(10,20,True)
        self.assertTrue(s.blocked)
        self.assertIn('상세 확인',s.hero_caption)
        self.assertEqual(u.color_for(s),u.RED)

    def test_tightest_window(self):
        self.assertEqual(codex(10,80).hero_percent,20)

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
        self.assertEqual(u.reset_stamp('9월 10일 19:52'),'19:52 재설정')
        self.assertEqual(u.reset_stamp(''),'')
        self.assertEqual(u.bar_color('chatgpt',46,False),u.CODEX)
        self.assertEqual(u.bar_color('chatgpt',18,False),u.WARN)
        self.assertEqual(u.bar_color('chatgpt',8,False),u.DANGER)
        self.assertEqual(u.bar_color('chatgpt',8,True),u.CODEX)
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
