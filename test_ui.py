import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import usage_widget as u
from providers import ProviderSnapshot,QuotaBar,error_snapshot

class UiTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        path=Path(self.directory.name)
        self.patches=[patch.object(u,'SETTINGS_PATH',path/'settings.json'),patch.object(u,'CACHE_PATH',path/'cache.json')]
        for item in self.patches:item.start()
        self.w=u.UsageWidget(preview=True)
        self.w.root.withdraw()

    def tearDown(self):
        self.w.close()
        for item in self.patches:item.stop()
        self.directory.cleanup()

    def test_error_shrinks_and_modes_round_trip(self):
        w=self.w
        s=ProviderSnapshot('chatgpt','Codex','Plus',True,80,'5시간 기준 잔여',bars=[QuotaBar('5시간',80,20,'','9월 10일 14:00')])
        w.snapshots['chatgpt']=s;w.render('chatgpt');w.root.update_idletasks()
        before=w.cards['chatgpt'].rows.winfo_reqheight()
        w.snapshots['chatgpt']=error_snapshot('chatgpt','Codex','조회 실패','');w.render('chatgpt');w.root.update_idletasks()
        self.assertLess(w.cards['chatgpt'].rows.winfo_reqheight(),before)
        mode=w.compact;w.toggle();self.assertNotEqual(w.compact,mode);w.toggle();self.assertEqual(w.compact,mode)

    def prepare(self):
        w=self.w;w.preview=False
        w.runner=Mock(slots={});w.runner.poll.return_value=[]
        w.watcher=Mock();w.watcher.changed.return_value=[]
        return w

    def test_lock_cancels_and_unlock_refreshes(self):
        w=self.prepare()
        with patch.object(u,'session_locked',return_value=True):w.tick()
        self.assertTrue(w.locked)
        self.assertEqual(w.runner.cancel.call_count,2)
        w.runner.start.assert_not_called()
        w.last_environment=float('-inf')
        with patch.object(u,'session_locked',return_value=False):w.tick()
        self.assertFalse(w.locked)
        self.assertEqual(w.runner.start.call_count,2)

    def test_provider_disable_prevents_poll(self):
        w=self.prepare()
        w.enabled['cursor'].set(False);w.toggle_provider('cursor')
        with patch.object(u,'session_locked',return_value=False):w.tick()
        self.assertEqual(w.runner.start.call_count,1)
        self.assertEqual(w.runner.start.call_args.args[0],'chatgpt')
        self.assertEqual(w.cards['cursor'].winfo_manager(),'')

    def test_disabled_provider_cache_stays_disabled(self):
        w=self.w
        w.enabled['cursor'].set(False)
        w.snapshots['cursor']=ProviderSnapshot('cursor','Cursor','Pro',True,80,'')
        w.render('cursor')
        self.assertEqual(w.mini_values['cursor'].cget('text'),'Cursor 꺼짐')
        self.assertEqual(w.mini_values['cursor'].winfo_manager(),'')

    def test_monitor_change_clamps_but_not_during_drag(self):
        w=self.prepare()
        with patch.object(u,'session_locked',return_value=False),patch.object(u,'monitor_area',return_value=(0,0,800,600)),patch.object(w,'place') as place:
            w.environment(0);self.assertEqual(place.call_count,1)
            w.dragging=True;w.last_area=None;w.environment(3)
            self.assertEqual(place.call_count,1)

    def test_auth_change_cancels_old_request(self):
        w=self.prepare();w.watcher.changed.return_value=['chatgpt']
        with patch.object(u,'session_locked',return_value=False):w.environment(0)
        w.runner.cancel.assert_called_once_with('chatgpt')
        self.assertEqual(w.due['chatgpt'],0)

    def test_scale_resizes_window_and_clamps(self):
        w=self.w
        s=ProviderSnapshot('chatgpt','Codex','Plus',True,80,'5시간 기준 잔여',bars=[QuotaBar('5시간',80,20,'','9월 10일 14:00')])
        w.snapshots['chatgpt']=s;w.render('chatgpt')
        base=int(w.shell.cget('width'))
        card_h=w.cards['chatgpt'].height
        chip_w=int(w.mini_values['chatgpt'].cget('width'))
        w.set_scale(1.3)
        self.assertEqual(w.scale, 1.3)
        self.assertEqual(int(w.shell.cget('width')), u.px(360, 1.3))
        self.assertGreater(int(w.shell.cget('width')), base)
        self.assertGreater(w.cards['chatgpt'].height, card_h)
        self.assertGreater(int(w.mini_values['chatgpt'].cget('width')), chip_w)
        w.set_scale(0.5)
        self.assertEqual(w.scale, 0.75)
        w.set_scale(9)
        self.assertEqual(w.scale, 1.5)
        w.nudge_scale(1)
        self.assertEqual(w.scale, 1.5)
        w.set_scale(1.0)
        self.assertEqual(w.scale, 1.0)
        self.assertEqual(int(w.shell.cget('width')), base)
        self.assertFalse(u.SETTINGS_PATH.exists())

    def test_scale_reclamps_position(self):
        w=self.w
        with patch.object(w, 'place') as place:
            w.set_scale(1.15)
            place.assert_called_once()
        self.assertFalse(u.SETTINGS_PATH.exists())

    def test_persist_includes_scale(self):
        w=self.w
        w.preview=False
        w.set_scale(1.15)
        self.assertEqual(u.read_json(u.SETTINGS_PATH).get('scale'), 1.15)

    def test_loads_saved_scale(self):
        self.w.close()
        u.save_json(u.SETTINGS_PATH, {'scale': 1.3})
        self.w=u.UsageWidget(preview=True)
        self.w.root.withdraw()
        self.assertEqual(self.w.scale, 1.3)
        self.assertEqual(int(self.w.shell.cget('width')), u.px(360, 1.3))

    def test_environment_skips_raise_while_overlay(self):
        w=self.prepare()
        w._overlay=1
        w.topmost.set(True)
        w.last_area=(0,0,800,600)
        w.root.geometry('+10+10')
        with patch.object(u,'session_locked',return_value=False),patch.object(u,'monitor_area',return_value=(0,0,800,600)),patch.object(u,'set_over_taskbar') as zorder:
            w.environment(0)
            zorder.assert_not_called()

    def test_popup_holds_overlay_until_menu_closes(self):
        w=self.prepare()
        w.topmost.set(True)
        event=type('E',(),{'x_root':10,'y_root':20})()
        seen=[]
        def fake_popup(*_a,**_k):
            seen.append(w._overlay)
            seen.append(w._menu_held)
        with patch.object(w.menu,'tk_popup',side_effect=fake_popup),patch.object(w.menu,'grab_release'),patch.object(w.menu,'winfo_ismapped',return_value=False):
            w.popup(event)
            w.root.update()
        self.assertEqual(seen,[1,True])
        self.assertEqual(w._overlay,0)
        self.assertFalse(w._menu_held)

    def test_version_in_menu_and_help(self):
        w=self.w
        labels=[]
        for i in range(w.menu.index('end')+1):
            if w.menu.type(i)=='command':
                labels.append(w.menu.entrycget(i,'label'))
        self.assertIn(f'버전 {u.APP_VERSION}', labels)
        self.assertIn(f'현재 버전 {u.APP_VERSION}', w.help_text())

if __name__=='__main__':unittest.main(verbosity=2)
