import tempfile
import time
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
        self._pill_animate=u.UpdatePill.animate
        self._card_animate=u.Card.animate
        self._chip_animate=u.Chip.animate
        u.UpdatePill.animate=False
        u.Card.animate=False
        u.Chip.animate=False
        self.w=u.UsageWidget(preview=True)
        self.w.root.withdraw()

    def tearDown(self):
        self.w.close()
        u.UpdatePill.animate=self._pill_animate
        u.Card.animate=self._card_animate
        u.Chip.animate=self._chip_animate
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

    def test_cursor_card_shows_billing_reset(self):
        w=self.w
        bars=[QuotaBar('자사 모델',80,20,'','9월 14일 09:00'),QuotaBar('API 사용량',70,30,'','9월 14일 09:00')]
        w.snapshots['cursor']=ProviderSnapshot('cursor','Cursor','Pro',True,80,'',bars=bars)
        w.render('cursor')
        with_reset=w.cards['cursor'].height
        w.snapshots['cursor']=ProviderSnapshot('cursor','Cursor','Pro',True,80,'',bars=[QuotaBar('자사 모델',80,20,'',''),QuotaBar('API 사용량',70,30,'','')])
        w.cards['cursor'].last_signature=None
        w.render('cursor')
        self.assertGreater(with_reset,w.cards['cursor'].height)

    def test_card_eases_bar_when_remaining_drops(self):
        w=self.w
        card=w.cards['chatgpt']
        u.Card.animate=True
        card.render(ProviderSnapshot('chatgpt','Codex','Plus',True,80,'',bars=[QuotaBar('5시간',80,20,'','')]))
        self.assertEqual(card._shown_pcts,[80])
        card.render(ProviderSnapshot('chatgpt','Codex','Plus',True,50,'',bars=[QuotaBar('5시간',50,50,'','')]))
        self.assertGreater(card._shown_pcts[0],50)
        self.assertLessEqual(card._shown_pcts[0],80)
        card._anim_t0=time.monotonic()-2
        card._anim_tick()
        self.assertEqual(card._shown_pcts,[50])
        card.render(ProviderSnapshot('chatgpt','Codex','Plus',True,80,'',bars=[QuotaBar('5시간',80,20,'','')]))
        self.assertLess(card._shown_pcts[0],80)
        self.assertGreaterEqual(card._shown_pcts[0],50)
        self.assertLessEqual(card._anim_ms, u.BAR_ANIM_MAX_MS)
        u.Card.animate=False
        card.render(ProviderSnapshot('chatgpt','Codex','Plus',True,20,'',bars=[QuotaBar('5시간',20,80,'','')]))
        self.assertEqual(card._shown_pcts,[20])

    def test_chip_eases_fill_when_remaining_drops(self):
        chip=self.w.mini_values['chatgpt']
        u.Chip.animate=True
        chip.configure(percent=80)
        self.assertEqual(chip.percent,80)
        chip.configure(percent=40)
        self.assertGreater(chip.percent,40)
        self.assertLessEqual(chip.percent,80)
        chip._anim_t0=time.monotonic()-2
        chip._anim_tick()
        self.assertEqual(chip.percent,40)
        chip.configure(percent=90)
        self.assertLess(chip.percent,90)
        self.assertGreaterEqual(chip.percent,40)
        chip._anim_t0=time.monotonic()-3
        chip._anim_tick()
        self.assertEqual(chip.percent,90)
        u.Chip.animate=False

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

    def test_environment_keeps_widget_while_overlay(self):
        w=self.prepare()
        w._overlay=1
        w.topmost.set(True)
        w.last_area=(0,0,800,600)
        w.root.geometry('+10+10')
        with patch.object(u,'session_locked',return_value=False),patch.object(u,'monitor_area',return_value=(0,0,800,600)),patch.object(u,'set_over_taskbar') as zorder,patch.object(u,'lift_owned_popups') as popups,patch.object(u,'keep_topmost_style'):
            w.environment(0)
            zorder.assert_not_called()
            popups.assert_called()

    def test_environment_does_not_restack_widget_while_menu_held(self):
        w=self.prepare()
        w._menu_held=True
        w.topmost.set(True)
        w.last_area=(0,0,800,600)
        w.root.geometry('+10+10')
        with patch.object(u,'session_locked',return_value=False),patch.object(u,'monitor_area',return_value=(0,0,800,600)),patch.object(u,'set_over_taskbar') as zorder,patch.object(u,'lift_menu_windows') as lift,patch.object(u,'keep_topmost_style'):
            w.environment(0)
            zorder.assert_not_called()
            lift.assert_called()

    def _dropped_topmost(self, zorder):
        return any((len(c.args)>1 and c.args[1] is False) or c.kwargs.get('on') is False for c in zorder.call_args_list)

    def test_popup_holds_overlay_until_menu_closes(self):
        w=self.prepare()
        w.topmost.set(True)
        event=type('E',(),{'x_root':10,'y_root':20})()
        seen=[]
        def fake_popup(*_a,**_k):
            seen.append(w._overlay)
            seen.append(w._menu_held)
        with patch.object(w,'push_overlay') as pushed,patch.object(w.menu,'tk_popup',side_effect=fake_popup),patch.object(w.menu,'grab_release'),patch.object(w.menu,'winfo_ismapped',return_value=False):
            w.popup(event)
            w.root.update()
        self.assertEqual(seen,[0,True])
        pushed.assert_not_called()
        self.assertEqual(w._overlay,0)
        self.assertFalse(w._menu_held)

    def test_compact_popup_keeps_over_taskbar(self):
        w=self.prepare()
        w.compact=True
        w.apply_mode()
        w.topmost.set(True)
        self.assertTrue(w.root.bind_all('<Button-3>'))
        event=type('E',(),{'x_root':10,'y_root':20})()
        with patch.object(u,'set_over_taskbar') as zorder,patch.object(u,'lift_menu_windows') as lift,patch.object(w.menu,'tk_popup'),patch.object(w.menu,'grab_release'),patch.object(w.menu,'winfo_ismapped',return_value=False):
            w.popup(event)
            w.root.update()
        self.assertEqual(w._overlay,0)
        self.assertFalse(self._dropped_topmost(zorder))
        self.assertTrue(zorder.called)
        lift.assert_called()

    def test_menu_pulse_does_not_restack_widget(self):
        w=self.prepare()
        w._menu_held=True
        w.topmost.set(True)
        with patch.object(u,'set_over_taskbar') as zorder,patch.object(u,'lift_menu_windows') as lift:
            w._lift_menu()
        zorder.assert_not_called()
        lift.assert_called()

    def test_notify_keeps_over_taskbar(self):
        w=self.prepare()
        w.topmost.set(True)
        depth=[]
        def fake():
            depth.append(w._overlay)
        with patch.object(u,'set_over_taskbar') as zorder,patch.object(u,'lift_owned_popups'):
            w.notify(fake)
        self.assertEqual(depth,[1])
        self.assertFalse(self._dropped_topmost(zorder))
        self.assertTrue(zorder.called)
        self.assertEqual(w._overlay,0)

    def test_update_check_dialog_keeps_over_taskbar(self):
        w=self.prepare()
        w.topmost.set(True)
        w.update_queue.put(('checked', None, True, True))
        with patch.object(u,'set_over_taskbar') as zorder,patch.object(u,'lift_owned_popups'),patch.object(u.messagebox,'showinfo',return_value='ok'):
            w.drain_update_queue()
        self.assertFalse(self._dropped_topmost(zorder))
        self.assertEqual(w._overlay,0)

    def test_update_pill_shows_only_when_pending(self):
        w=self.w
        w.root.update_idletasks()
        self.assertEqual(w.update_pill.winfo_manager(),'')
        self.assertEqual(w.mini_pill.winfo_manager(),'')
        w.update_info={'version':'9.9.9','zip':'https://example.com/a.zip','notes':''}
        w.set_update_chrome()
        self.assertEqual(w.update_pill.winfo_manager(),'place')
        self.assertIn('9.9.9',w.update_pill.text)
        self.assertTrue(w.update_pill.ready)
        self.assertEqual(w.update_pill.cget('cursor'),'hand2')
        self.assertLessEqual(w.update_pill.width_px,u.px(270,1.0)-u.px(10,1.0)-u.px(84,1.0))
        w.compact=True;w.apply_mode()
        self.assertEqual(w.update_pill.winfo_manager(),'')
        self.assertEqual(w.mini_pill.winfo_manager(),'place')
        self.assertEqual(w.mini_title.winfo_manager(),'')
        self.assertLessEqual(w.mini_pill.width_px,u.px(76,1.0)-u.px(6,1.0)-u.px(12,1.0))
        w._update_busy=True;w.set_update_chrome()
        self.assertFalse(w.mini_pill.ready)
        self.assertEqual(w.mini_pill.cget('cursor'),'arrow')
        self.assertIn('설치',w.mini_pill.text)
        w._update_busy=False;w.update_info=None;w.set_update_chrome()
        self.assertEqual(w.mini_pill.winfo_manager(),'')
        self.assertEqual(w.mini_title.winfo_manager(),'place')
        w.set_scale(1.3)
        w.update_info={'version':'9.9.9','zip':'https://example.com/a.zip','notes':''}
        w.set_update_chrome()
        self.assertEqual(w.mini_pill.cget('height'),str(u.px(22,1.3)))

    def test_update_pill_pulse_starts_and_stops(self):
        w=self.w
        u.UpdatePill.animate=True
        w.update_info={'version':'9.9.9','zip':'https://example.com/a.zip','notes':''}
        w.set_update_chrome()
        self.assertTrue(w.update_pill.ready)
        self.assertIsNotNone(w.update_pill._pulse_after)
        self.assertIsNone(w.mini_pill._pulse_after)
        w.update_pill.hide()
        self.assertIsNone(w.update_pill._pulse_after)
        w.set_update_chrome()
        self.assertIsNotNone(w.update_pill._pulse_after)
        w._update_busy=True
        w.set_update_chrome()
        self.assertFalse(w.update_pill.ready)
        self.assertIsNone(w.update_pill._pulse_after)
        w._update_busy=False
        w.compact=True
        w.apply_mode()
        self.assertTrue(w.mini_pill.ready)
        self.assertIsNotNone(w.mini_pill._pulse_after)
        self.assertIsNone(w.update_pill._pulse_after)
        w.mini_pill.hide()
        self.assertIsNone(w.mini_pill._pulse_after)

    def test_update_pill_has_tip_when_pending(self):
        w=self.w
        self.assertEqual(w.update_pill.tip_text, '')
        self.assertIs(w.update_pill.tip, w.tip)
        self.assertIs(w.mini_pill.tip, w.tip)
        w.update_info={'version':'9.9.9','zip':'https://example.com/a.zip','notes':''}
        w.set_update_chrome()
        self.assertEqual(w.update_pill.tip_text, '업데이트 9.9.9')
        w.update_pill.hide()
        self.assertEqual(w.update_pill.tip_text, '')
        w.set_update_chrome()
        w._update_busy=True
        w.set_update_chrome()
        self.assertEqual(w.update_pill.tip_text, '설치 중...')
        w._update_busy=False
        w.compact=True
        w.apply_mode()
        self.assertEqual(w.mini_pill.tip_text, '업데이트 9.9.9')
        w.mini_pill.hide()
        self.assertEqual(w.mini_pill.tip_text, '')

    def test_version_in_menu_and_help(self):
        w=self.w
        labels=[]
        for i in range(w.menu.index('end')+1):
            if w.menu.type(i)=='command':
                labels.append(w.menu.entrycget(i,'label'))
        self.assertIn(f'버전 {u.APP_VERSION}', labels)
        self.assertEqual(labels[labels.index(f'버전 {u.APP_VERSION}')-1], '바탕화면 바로가기 생성')
        help_text = w.help_text()
        self.assertIn(f'현재 버전 {u.APP_VERSION}', help_text)
        self.assertIn('제휴되지 않은 비공식', help_text)
        self.assertIn('실패하거나 바뀔 수 있습니다', help_text)
        self.assertIn('AI Usage.exe', help_text)

    def test_place_on_screen_center_uses_monitor_not_widget(self):
        w=self.w
        w.root.geometry('+16+24')
        w.root.update_idletasks()
        dialog=u.tk.Toplevel(w.root)
        dialog.geometry('200x100+16+24')
        dialog.update_idletasks()
        width=max(dialog.winfo_reqwidth(),dialog.winfo_width(),1)
        height=max(dialog.winfo_reqheight(),dialog.winfo_height(),1)
        with patch.object(u,'work_area',return_value=(0,0,1000,800)):
            expected=u.center_box(width,height,16,24)
            u.place_on_screen_center(dialog,16,24)
        dialog.update_idletasks()
        self.assertTrue(dialog.geometry().endswith(f'+{expected[0]}+{expected[1]}'), dialog.geometry())
        self.assertNotEqual((dialog.winfo_rootx(),dialog.winfo_rooty()),(16,24))
        dialog.destroy()

    def test_notify_parents_messagebox_to_screen_center_owner(self):
        w=self.w
        w.root.geometry('+16+24')
        w.root.update_idletasks()
        seen=[]
        def fake_info(*args,**kwargs):
            seen.append(kwargs.get('parent'))
            return 'ok'
        with patch.object(u,'work_area',return_value=(0,0,1000,800)),patch.object(u.messagebox,'showinfo',side_effect=fake_info):
            w.notify(u.messagebox.showinfo,'업데이트','이미 최신입니다.',parent=w.root)
        self.assertEqual(len(seen),1)
        self.assertIs(seen[0],w._center_owner)
        self.assertIsNot(seen[0],w.root)

    def test_menu_checkmark_is_white(self):
        self.assertEqual(str(self.w.menu.cget('selectcolor')).upper(), '#FFFFFF')

    def test_icon_buttons_have_hints(self):
        w=self.w
        self.assertEqual(w.header_buttons[0].tip_text, '새로고침 (F5)')
        self.assertEqual(w.header_buttons[1].tip_text, '한 줄로 접기')
        self.assertEqual(w.header_buttons[2].tip_text, '종료')
        self.assertEqual(w.mini_buttons[0].tip_text, '새로고침 (F5)')
        self.assertEqual(w.mini_buttons[1].tip_text, '상세로 펼치기')
        self.assertEqual(w.mini_buttons[2].tip_text, '종료')

    def test_tip_shows_after_schedule(self):
        w=self.w
        w.tip.delay=0
        btn=w.header_buttons[0]
        w.tip.schedule(btn, btn.tip_text)
        w.root.update()
        self.assertIsNotNone(w.tip.win)
        self.assertEqual(w.tip.win.winfo_children()[0].cget('text'), '새로고침 (F5)')
        self.assertIsNotNone(w.tip._keep)
        w.tip.hide()
        self.assertIsNone(w.tip.win)
        self.assertIsNone(w.tip._keep)

    def _assert_tip_lifted_after_widget(self, zorder, tip_win):
        windows=[c.args[0] for c in zorder.call_args_list]
        self.assertIn(self.w.root, windows)
        self.assertIn(tip_win, windows)
        self.assertGreater(windows.index(tip_win), windows.index(self.w.root))
        self.assertFalse(self._dropped_topmost(zorder))

    def test_tip_stays_above_after_environment_raise(self):
        w=self.prepare()
        w.topmost.set(True)
        w.last_area=(0,0,800,600)
        w.root.geometry('+10+10')
        w.tip.delay=0
        btn=w.header_buttons[0]
        w.tip.schedule(btn, btn.tip_text)
        w.root.update()
        tip=w.tip.win
        self.assertIsNotNone(tip)
        self.assertTrue(tip.winfo_ismapped())
        with patch.object(u,'session_locked',return_value=False),patch.object(u,'monitor_area',return_value=(0,0,800,600)),patch.object(u,'set_over_taskbar') as zorder,patch.object(u,'lift_tip_window',wraps=u.lift_tip_window) as lift:
            w.environment(0)
            self.assertTrue(tip.winfo_ismapped())
            self.assertIs(w.tip.win, tip)
            lift.assert_called()
            self._assert_tip_lifted_after_widget(zorder, tip)

    def test_update_pill_tip_stays_above_after_apply_topmost(self):
        w=self.prepare()
        w.topmost.set(True)
        w.update_info={'version':'9.9.9','zip':'https://example.com/a.zip','notes':''}
        w.set_update_chrome()
        w.tip.delay=0
        w.tip.schedule(w.update_pill, w.update_pill.tip_text)
        w.root.update()
        tip=w.tip.win
        self.assertIsNotNone(tip)
        self.assertEqual(tip.winfo_children()[0].cget('text'), '업데이트 9.9.9')
        with patch.object(u,'set_over_taskbar') as zorder,patch.object(u,'lift_tip_window',wraps=u.lift_tip_window) as lift:
            w.apply_topmost()
            self.assertTrue(tip.winfo_ismapped())
            self.assertIs(w.tip.win, tip)
            lift.assert_called()
            self._assert_tip_lifted_after_widget(zorder, tip)

    def test_install_update_asks_with_notes_then_cancels(self):
        w=self.prepare()
        w.update_info={'version':'9.9.9','zip':'https://example.com/a.zip','notes':'체크표시를 흰색으로 바꿈'}
        seen=[]
        def fake_notify(fn,*args,**kwargs):
            seen.append((fn, args[0], args[1]))
            return False
        w.notify=fake_notify
        w.install_update()
        self.assertEqual(seen[0][0], u.messagebox.askyesno)
        self.assertEqual(seen[0][1], '업데이트')
        self.assertIn('새 버전 9.9.9', seen[0][2])
        self.assertIn('체크표시를 흰색으로 바꿈', seen[0][2])
        self.assertFalse(w._update_busy)

if __name__=='__main__':unittest.main(verbosity=2)
