import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, call
import usage_widget as u
from providers import ProviderSnapshot,QuotaBar,QuotaItem,error_snapshot


def limit(raw_id,name,remaining,window=None,reset_at=None,source='chatgpt',scope='global',category='main'):
    """Canonical quota fixture. Identity and meaning never come from `name`."""
    return QuotaItem(f'{source}:{category}:{raw_id}',source,category,name,raw_identifier=raw_id,
                     window_seconds=window,window_label=name,
                     used_percent=None if remaining is None else 100-remaining,
                     remaining_percent=remaining,reset_at=reset_at,scope=scope)


FIVE_H=18000.0
WEEK=604800.0

class UiTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        path=Path(self.directory.name)
        self.patches=[patch.object(u,'SETTINGS_PATH',path/'settings.json'),patch.object(u,'CACHE_PATH',path/'cache.json')]
        for item in self.patches:item.start()
        self._pill_animate=u.UpdatePill.animate
        u.UpdatePill.animate=False
        self.w=u.UsageWidget(preview=True)
        self.w.root.withdraw()

    def tearDown(self):
        self.w.close()
        u.UpdatePill.animate=self._pill_animate
        for item in self.patches:item.stop()
        self.directory.cleanup()

    def test_error_shrinks_and_modes_round_trip(self):
        w=self.w
        s=ProviderSnapshot('chatgpt','Codex','Plus',True,80,'5시간 기준 잔여',bars=[QuotaBar('5시간',80,20,'','9월 10일 14:00')])
        w.snapshots['chatgpt']=s;w.render('chatgpt');w.root.update_idletasks()
        before=w.cards['chatgpt'].rows.winfo_reqheight()
        w.snapshots['chatgpt']=error_snapshot('chatgpt','Codex','조회 실패','');w.render('chatgpt');w.root.update_idletasks()
        card=w.cards['chatgpt']
        self.assertTrue(any(card.rows.itemcget(item,'text')=='조회 실패'
                            for item in card.rows.find_all() if card.rows.type(item)=='text'))
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
        self.assertCountEqual(w.runner.cancel.call_args_list,[call(key) for key in u.FETCHERS])
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
        self.assertEqual(int(w.shell.cget('width')), u.px(u.WINDOW_W, 1.3))
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
        self.assertEqual(int(self.w.shell.cget('width')), u.px(u.WINDOW_W, 1.3))

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

    def menu_labels(self, menu):
        return [None if menu.type(i)=='separator' else menu.entrycget(i,'label') for i in range(menu.index('end')+1)]

    def test_menu_holds_only_what_has_no_other_control(self):
        self.assertEqual(self.menu_labels(self.w.menu), [
            '크기', None,
            '서비스·로그인 관리...', '사용량 페이지 열기', None,
            '항상 위', 'Windows 시작 시 실행', '한도 임박·소진 알림', None,
            f'업데이트 확인 ({u.APP_VERSION})...', '도움말 · 표시 기준...', '바탕화면 바로가기 만들기', None,
            '종료',
        ])

    def test_usage_pages_follow_enabled_services(self):
        w=self.w
        for key in u.FETCHERS: w.enabled[key].set(key!='cursor')
        w._fill_usage_pages()
        self.assertEqual(self.menu_labels(w.usage_pages), ['GPT','Claude'])
        for key in u.FETCHERS: w.enabled[key].set(False)
        w._fill_usage_pages()
        self.assertEqual(self.menu_labels(w.usage_pages), ['켜 둔 서비스 없음'])
        self.assertEqual(w.usage_pages.entrycget(0,'state'), 'disabled')

    def test_one_update_entry_checks_then_installs(self):
        w=self.w
        index=w._update_menu
        w.update_info={'version':'9.9.9','zip':'https://example.com/a.zip','notes':''}
        w.set_update_chrome()
        self.assertEqual(w.menu.entrycget(index,'label'), '업데이트 9.9.9 설치...')
        w.update_info=None
        w.set_update_chrome()
        self.assertEqual(w.menu.entrycget(index,'label'), f'업데이트 확인 ({u.APP_VERSION})...')

    def test_turning_alerts_on_sends_one_sample(self):
        w=self.w
        w.toast=Mock()
        w.notifications.set(False); w.toggle_notifications()
        w.toast.send.assert_not_called()
        w.notifications.set(True); w.toggle_notifications()
        self.assertEqual(w.toast.send.call_count, 1)
        w.toast=None

    def test_version_in_menu_and_help(self):
        w=self.w
        self.assertIn(f'업데이트 확인 ({u.APP_VERSION})...', self.menu_labels(w.menu))
        help_text = w.help_text()
        self.assertIn(f'현재 버전 {u.APP_VERSION}', help_text)
        self.assertIn('제휴되지 않은 비공식', help_text)
        self.assertIn('실패하거나 바뀔 수 있습니다', help_text)
        self.assertIn('AI Usage.exe', help_text)

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

    def test_cursor_quota_fallback_does_not_drive_visual_activity(self):
        w=self.w
        monitor=Mock()
        monitor.visual_active.return_value=False
        w.cursor_activity=monitor
        w.usage_until['cursor']=1000
        w._sync_activity_ui(10)
        self.assertFalse(w.cards['cursor']._desired_active)
        self.assertFalse(w.mini_values['cursor']._desired_active)
        monitor.visual_active.return_value=True
        w._sync_activity_ui(11)
        self.assertTrue(w.cards['cursor']._desired_active)
        self.assertTrue(w.mini_values['cursor']._desired_active)

    def test_unmapped_shimmer_preserves_desired_activity(self):
        chip=self.w.mini_values['cursor']
        self.assertFalse(chip.winfo_ismapped())
        chip.set_activity(True)
        chip._pause_shimmer()
        self.assertTrue(chip._desired_active)
        self.assertTrue(chip._active)
        chip.set_activity(False)
        self.assertFalse(chip._desired_active)

    def test_compact_chip_uses_fixed_28px_canvas(self):
        w=self.w
        w.compact=True
        w.apply_mode()
        chip=w.mini_values['chatgpt']
        self.assertEqual(int(chip.cget('height')), w.metrics.p(28))
        self.assertEqual(int(chip.place_info()['height']), w.metrics.p(28))
        self.assertEqual(w.metrics.chip_h, w.metrics.p(24))

    def test_busy_worker_coalesces_fast_refresh(self):
        w=self.prepare()
        w.runner.slots={'cursor':object()}
        w._request_fast_poll('cursor', 10)
        self.assertTrue(w.poll_pending['cursor'])
        w.runner.slots={}
        w.due['cursor']=100
        w.request_started['cursor']=9
        w._request_fast_poll('cursor', 10)
        self.assertEqual(w.due['cursor'], 11)

    def test_legacy_quota_cache_is_not_loaded(self):
        from providers import snapshot_to_dict
        snap=ProviderSnapshot('chatgpt','GPT','Plus',True,100,'5시간 기준 잔여',
                              bars=[QuotaBar('5시간',100,0,'')])
        u.save_json(u.CACHE_PATH, {'version':2,'chatgpt':snapshot_to_dict(snap)})
        self.w.load_cache()
        self.assertNotIn('chatgpt',self.w.snapshots)

    def test_cursor_badge_reads_the_most_limiting_main_quota(self):
        snap=ProviderSnapshot('cursor','Cursor','Pro',True,None,'Cursor Models 기준 잔여',
                             main_limits=[limit('autoPercentUsed','Cursor Models',90,source='cursor'),
                                          limit('apiPercentUsed','Other Models',10,source='cursor')])
        self.w.snapshots['cursor']=snap
        self.w.render('cursor')
        card=self.w.cards['cursor']
        self.assertEqual(card.rows.itemcget('severity','text'),'임박')
        self.assertEqual(card.rows.itemcget('hero','text'),'90%')

    def test_cursor_total_never_reaches_the_badge_or_the_hero(self):
        # totalPercentUsed is reference data. Whatever it says, the card reads
        # canonical quota.
        snap=ProviderSnapshot('cursor','Cursor','Pro',True,None,'Cursor Models 기준 잔여',
                             main_limits=[limit('autoPercentUsed','Cursor Models',90,source='cursor')],
                             internal={'totalPercentUsed':90})
        self.w.snapshots['cursor']=snap
        self.w.render('cursor')
        card=self.w.cards['cursor']
        self.assertEqual(card.rows.itemcget('severity','text'),'여유')
        self.assertEqual(card.rows.itemcget('hero','text'),'90%')
        self.assertEqual(u.representative_state(snap),'ok')
        for total in (0,55,100):
            snap.internal['totalPercentUsed']=total
            self.w.render('cursor')
            self.assertEqual(card.rows.itemcget('severity','text'),'여유')
            self.assertEqual(card.rows.itemcget('hero','text'),'90%')
            self.assertEqual(self.w.mini_values['cursor'].cget('text'),'Cursor 90%')

    def test_weekly_warning_and_exhaustion_keep_five_hour_hero(self):
        from tests.test_widget import codex
        for weekly, label in ((95,'임박'),(100,'주간 소진')):
            with self.subTest(weekly=weekly):
                snap=codex(10,weekly)
                self.w.snapshots['chatgpt']=snap
                self.w.render('chatgpt')
                card=self.w.cards['chatgpt']
                self.assertEqual(card.rows.itemcget('severity','text'),label)
                self.assertEqual(card.rows.itemcget('hero','text'),'90%')
                self.assertTrue(card.rows.find_withtag('strip'))
                snap.stale=True
                self.w.render('chatgpt')
                self.assertEqual(card.rows.itemcget('severity','text'),'이전 데이터')
                self.assertFalse(card.rows.find_withtag('strip'))

    def test_server_restriction_badge_explains_unknown_limit(self):
        from tests.test_widget import codex
        self.w.snapshots['chatgpt']=codex(10,20,True)
        self.w.render('chatgpt')
        card=self.w.cards['chatgpt']
        self.assertEqual(card.rows.itemcget('severity','text'),'사용 제한 · 상세 확인')
        self.assertEqual(card.rows.itemcget('hero','text'),'90%')

    def test_weekly_only_quota_drives_ring_title_reset_and_compact_value(self):
        w=self.w
        reset=u.time.mktime((2025,9,24,12,0,0,0,0,-1))
        snap=ProviderSnapshot('chatgpt','GPT','ChatGPT Plus',True,42,'주간 기준 잔여',
            main_limits=[limit('secondary_window','주간',42,WEEK,reset)],fetched_at=reset-86400)
        w.snapshots['chatgpt']=snap
        w.render('chatgpt')
        card=w.cards['chatgpt']
        texts=[card.rows.itemcget(item,'text') for item in card.rows.find_all()
               if card.rows.type(item)=='text']
        self.assertIn('주간 한도 · 남은 사용량',texts)
        self.assertNotIn('5시간 한도 · 남은 사용량',texts)
        self.assertEqual(card.rows.itemcget('hero','text'),'42%')
        self.assertEqual(w.mini_values['chatgpt'].cget('text'),'GPT 42%')
        card.refresh_clock(card._reset_epoch-7*86400)
        self.assertEqual('7일 후',card.rows.itemcget('countdown','text'))
        self.assertIn('9월 24일 12:00 리셋',texts)

    def test_five_hour_window_is_consistent_across_ring_and_compact_ui(self):
        w=self.w
        snap=ProviderSnapshot('chatgpt','GPT','ChatGPT Plus',True,10,'5시간 기준 잔여',
            main_limits=[limit('primary_window','5시간',80,FIVE_H),
                         limit('secondary_window','주간',10,WEEK)])
        w.snapshots['chatgpt']=snap
        w.render('chatgpt')
        card=w.cards['chatgpt']
        texts=[card.rows.itemcget(item,'text') for item in card.rows.find_all()
               if card.rows.type(item)=='text']
        self.assertIn('5시간 한도 · 남은 사용량',texts)
        self.assertIn('주간 한도',texts)
        self.assertEqual(card.rows.itemcget('hero','text'),'80%')
        self.assertEqual(w.mini_values['chatgpt'].cget('text'),'GPT 80%')

    def test_claude_card_draws_every_canonical_window(self):
        snap=ProviderSnapshot('claude','Claude','Claude',True,80,'5시간 기준 잔여',
            main_limits=[limit('five_hour','5시간',80,FIVE_H,source='claude'),
                         limit('seven_day','주간',40,WEEK,1790000000.0,source='claude')])
        self.w.snapshots['claude']=snap
        self.w.enabled['claude'].set(True)
        self.w.render('claude')
        card=self.w.cards['claude']
        texts=[card.rows.itemcget(item,'text') for item in card.rows.find_all()
               if card.rows.type(item)=='text']
        self.assertEqual(card.rows.itemcget('hero','text'),'80%')
        self.assertIn('5시간 한도 · 남은 사용량',texts)
        self.assertIn('주간 한도',texts)
        self.assertEqual(self.w.mini_values['claude'].cget('text'),'Claude 80%')

    def test_cursor_secondary_severity_does_not_change_hero_or_compact(self):
        for remaining, expected in ((80,u.blend(u.CARD,u.CURSOR,.7)), (25,u.WARN), (10,u.DANGER), (3,'#DC2626')):
            with self.subTest(remaining=remaining):
                snap=ProviderSnapshot('cursor','Cursor','Pro',True,80,'Cursor Models 기준 잔여',
                    main_limits=[limit('autoPercentUsed','Cursor Models',80,source='cursor'),
                                 limit('apiPercentUsed','Other Models',remaining,source='cursor')])
                self.w.snapshots['cursor']=snap
                with patch.object(u,'progress_photo',wraps=u.progress_photo) as photo:
                    self.w.render('cursor')
                self.assertTrue(any(c.args[5] == expected for c in photo.call_args_list))
                card=self.w.cards['cursor']
                self.assertEqual(card.rows.itemcget('hero','text'),'80%')
                self.assertEqual(card.rows.itemcget('hero','fill'),u.CURSOR)
                self.assertEqual(self.w.mini_values['cursor'].cget('text'),'Cursor 80%')
                self.assertEqual(u.representative_state(snap),'ok')

    def test_internal_only_update_keeps_card_and_additional_drawing(self):
        from dataclasses import replace
        snap=ProviderSnapshot('chatgpt','GPT','Plus',True,80,'5시간 기준 잔여',
                             bars=[QuotaBar('5시간',80,20,'')])
        self.w.snapshots['chatgpt']=snap
        self.w.render('chatgpt')
        card=self.w.cards['chatgpt']
        updated=replace(snap,internal={'debug':42,'quota_observed_at':1234},fetched_at=snap.fetched_at+2)
        with patch.object(card,'_paint',wraps=card._paint) as paint, patch.object(card.additional,'render',wraps=card.additional.render) as extra:
            self.w.snapshots['chatgpt']=updated
            self.w.render('chatgpt')
            paint.assert_not_called()
            extra.assert_not_called()
        self.assertIs(card._snap,updated)

    def test_visible_updates_still_repaint(self):
        from dataclasses import replace
        five,week=limit('primary_window','5시간',80,FIVE_H),limit('secondary_window','주간',70,WEEK)
        snap=ProviderSnapshot('chatgpt','GPT','Plus',True,80,'5시간 기준 잔여',main_limits=[five,week])
        card=self.w.cards['chatgpt']
        card.render(snap)
        changes=[replace(snap,main_limits=[limit('primary_window','5시간',79,FIVE_H),week],bars=[]),
                 replace(snap,main_limits=[five,limit('secondary_window','주간',69,WEEK)],bars=[]),
                 replace(snap,stale=True), replace(snap,plan='Changed'),
                 replace(snap,main_limits=[limit('primary_window','5시간',80,FIVE_H,1758553200.0),week],bars=[])]
        for changed in changes:
            with patch.object(card,'_paint',wraps=card._paint) as paint:
                card.render(changed)
                paint.assert_called_once()

    def test_internal_reference_value_never_repaints_the_card(self):
        from dataclasses import replace
        snap=ProviderSnapshot('cursor','Cursor','Pro',True,None,'Cursor Models 기준 잔여',
            main_limits=[limit('autoPercentUsed','Cursor Models',80,source='cursor')],
            internal={'totalPercentUsed':10})
        card=self.w.cards['cursor']
        card.render(snap)
        before=card.last_signature
        with patch.object(card,'_paint',wraps=card._paint) as paint:
            card.render(replace(snap,internal={'totalPercentUsed':95},bars=[]))
            paint.assert_not_called()
        self.assertEqual(card.last_signature,before)
        self.assertEqual(card.rows.itemcget('severity','text'),'여유')

    def test_activity_still_paints_effect_without_snapshot_change(self):
        card=self.w.cards['chatgpt']
        card.render(ProviderSnapshot('chatgpt','GPT','Plus',True,80,'5시간 기준 잔여',bars=[QuotaBar('5시간',80,20,'')]))
        with patch.object(card,'animate',True), patch.object(card,'winfo_ismapped',return_value=True), patch.object(card,'_start_shimmer'), patch.object(card,'_paint_shimmer') as paint:
            card.set_activity(True)
            card._shimmer_tick()
            paint.assert_called_once()

    def test_stale_statusline_allows_cli_fallback(self):
        snap=ProviderSnapshot('claude','Claude','Claude',True,80,'5시간 기준 잔여',stale=True)
        w=self.prepare()
        w.claude_cli_due=0
        with patch.object(u,'fetch_claude',return_value=snap):
            w.start_claude_job()
        w.runner.start.assert_called_once()
        self.assertEqual(w.runner.start.call_args.args[0],'claude')

    def test_cached_cli_display_does_not_restamp_freshness(self):
        snap=ProviderSnapshot('claude','Claude','Claude',True,80,'5시간 기준 잔여',
            fetched_at=1000,internal={'source':'claude_cli','quota_observed_at':1000})
        self.w.claude_cli_snapshot=snap
        self.w.claude_cli_at=10
        missing=error_snapshot('claude','Claude','waiting','')
        first=self.w._claude_display_snapshot(missing,20)
        later=self.w._claude_display_snapshot(missing,311)
        self.assertFalse(first.stale)
        self.assertTrue(later.stale)
        self.assertEqual(first.fetched_at,later.fetched_at)
        self.assertEqual(self.w.claude_cli_at,10)
        self.assertEqual(later.internal['quota_observed_at'],1000)

    def test_active_interval_setting_controls_production_schedule(self):
        from polling import PollingPolicy
        w=self.prepare()
        w.request_started['chatgpt']=10
        snap=ProviderSnapshot('chatgpt','GPT','Plus',True,80,'')
        with patch('polling.policy_for',return_value=PollingPolicy(active_interval=4)):
            w._schedule_poll('chatgpt',snap,11,active=True)
        self.assertEqual(w.due['chatgpt'],14)

    def test_visible_additional_updates_repaint(self):
        from dataclasses import replace
        from providers import LimitGroup, QuotaItem
        item=QuotaItem('extra:1','chatgpt','additional','weekly',remaining_percent=80)
        group=LimitGroup('extra','chatgpt','Extra',limits=[item])
        snap=ProviderSnapshot('chatgpt','GPT','Plus',True,80,'',additional_groups=[group])
        card=self.w.cards['chatgpt']
        card.render(snap)
        updated=replace(snap,additional_groups=[replace(group,limits=[replace(item,remaining_percent=70)])])
        with patch.object(card,'_paint',wraps=card._paint) as paint:
            card.render(updated)
            paint.assert_called_once()
        self.assertEqual(card.additional._rows[-1].percent,70)

    def test_status_copy_names_the_cause_and_keeps_waiting_quiet(self):
        login=ProviderSnapshot('cursor','Cursor','Pro',False,None,'',error='Cursor에 다시 로그인하세요.')
        self.assertEqual(u.failure_cause(login),'재로그인 필요')
        self.assertEqual(u.failure_cause(ProviderSnapshot('chatgpt','GPT','-',False,None,'',error='사용량 응답 시간이 초과되었습니다.')),'응답 시간 초과')
        self.assertEqual(u.failure_cause(ProviderSnapshot('claude','Claude','-',False,None,'',error='서버 연결을 확인한 뒤 다시 시도하세요.')),'연결 확인 필요')
        waiting=ProviderSnapshot('claude','Claude','-',False,None,'',error='Claude Code에서 사용량을 가져오는 중입니다.')
        self.assertEqual(u.failure_cause(waiting),'확인 중')
        self.assertFalse(u.service_needs_actions(waiting))
        self.assertEqual(u.service_status_text(ProviderSnapshot('chatgpt','GPT','Plus',True,80,'',fetched_at=1000),1020),'20초 전 확인')
        stale=ProviderSnapshot('chatgpt','GPT','Plus',True,80,'',stale=True,error='Codex CLI에 로그인해 주세요.',fetched_at=1000)
        self.assertEqual(u.service_status_text(stale,1020),'20초 전 확인 · 이전 값 · 재로그인 필요')
        self.assertTrue(u.service_needs_actions(stale))
        self.assertGreaterEqual(_contrast(u.STATUS_FG, u.CARD), 4.5)

    def test_header_follows_the_oldest_service(self):
        now=1_700_000_020
        fresh=ProviderSnapshot('chatgpt','GPT','Plus',True,80,'',fetched_at=now-2)
        older=ProviderSnapshot('cursor','Cursor','Pro',True,80,'',fetched_at=now-20)
        distant=ProviderSnapshot('cursor','Cursor','Pro',True,80,'',fetched_at=now-10*86400)
        self.assertEqual(u.header_freshness([fresh,older],now),('20초 전 확인',20))
        self.assertEqual(u.header_freshness([fresh,distant],now)[0],'서비스별 확인')
        failed=ProviderSnapshot('claude','Claude','-',False,None,'',error='조회 실패',fetched_at=now)
        self.assertEqual(u.header_freshness([failed],now)[0],'확인 실패')

    def test_each_card_shows_its_own_check_and_cached_error(self):
        from types import SimpleNamespace
        w=self.w
        stamp=1_700_000_000
        w.enabled['cursor'].set(True)
        w.snapshots['chatgpt']=ProviderSnapshot('chatgpt','GPT','Plus',True,91,'',fetched_at=stamp,
            main_limits=[limit('primary_window','5시간',91,FIVE_H)])
        w.snapshots['cursor']=ProviderSnapshot('cursor','Cursor','Pro',True,40,'',fetched_at=stamp-10*86400,stale=True,
            error='Cursor에 다시 로그인하세요.',
            main_limits=[limit('autoPercentUsed','Cursor Models',40,source='cursor')])
        w.render('chatgpt')
        w.render('cursor')
        gpt, cursor=w.cards['chatgpt'], w.cards['cursor']
        gpt.refresh_clock(stamp+20)
        cursor.refresh_clock(stamp+20)
        self.assertEqual(gpt.rows.itemcget('service_status','text'),'20초 전 확인')
        self.assertFalse(gpt._actions)
        self.assertEqual(cursor.rows.itemcget('service_status','text'),'10일 전 확인 · 이전 값 · 재로그인 필요')
        self.assertIn('Cursor에 다시 로그인하세요.',
                      [cursor.rows.itemcget(item,'text') for item in cursor.rows.find_withtag('error_text')
                       if cursor.rows.type(item)=='text'])
        self.assertEqual(sorted(kind for *_, kind in cursor._actions),['login','retry'])
        with patch.object(u.time,'time',return_value=stamp+20):
            w.refresh_design_status()
        self.assertIn('서비스별 확인', w.updated_label.cget('text'))
        self.assertNotIn('방금', w.updated_label.cget('text'))
        signature=cursor.last_signature
        cursor.refresh_clock(stamp+50)
        self.assertEqual(cursor.last_signature, signature)
        self.assertIn('10일 전 확인', cursor.rows.itemcget('service_status','text'))
        cursor.set_collapsed(True)
        self.assertIn('재로그인 필요', cursor.rows.itemcget('service_status','text'))
        self.assertTrue(cursor._actions)
        login=next(box for box in cursor._actions if box[4]=='login')
        with patch.object(w,'notify',return_value=False) as notify, patch.object(u.webbrowser,'open') as browser:
            cursor._clicked(SimpleNamespace(x=(login[0]+login[2])/2, y=(login[1]+login[3])/2))
            browser.assert_not_called()
        self.assertEqual(notify.call_args.args[1],'로그인 안내')
        self.assertIn('다시 로그인', notify.call_args.args[2])
        if w.timer is not None:
            w.root.after_cancel(w.timer)
            w.timer=None
        w.preview=False
        w.locked=False
        w.runner=Mock()
        w.runner.start.return_value=True
        w.runner.slots={}
        retry=next(box for box in cursor._actions if box[4]=='retry')
        with patch.object(u.webbrowser,'open') as browser:
            cursor._clicked(SimpleNamespace(x=(retry[0]+retry[2])/2, y=(retry[1]+retry[3])/2))
            browser.assert_not_called()
        w.runner.start.assert_called_once()
        self.assertEqual(w.runner.start.call_args.args[0],'cursor')
        w.snapshots['chatgpt']=error_snapshot('chatgpt','GPT','조회 실패','')
        w.render('chatgpt')
        self.assertIn('조회 실패',[gpt.rows.itemcget(item,'text') for item in gpt.rows.find_withtag('error_text')
                                  if gpt.rows.type(item)=='text'])
        self.assertTrue(gpt._actions)
        self.assertNotIn('조회 실패', cursor.rows.itemcget('service_status','text'))

    def test_failed_refresh_keeps_the_cause_on_the_cached_card(self):
        w=self.w
        w.snapshots['chatgpt']=ProviderSnapshot(
            'chatgpt','GPT','Plus',True,80,'',fetched_at=1_700_000_000,
            main_limits=[limit('primary_window','5시간',80,FIVE_H)])
        w.accept('chatgpt', error_snapshot('chatgpt','GPT','Codex CLI에 로그인해 주세요.',''))
        snap=w.snapshots['chatgpt']
        self.assertTrue(snap.ok and snap.stale)
        self.assertEqual(snap.fetched_at, 1_700_000_000)
        card=w.cards['chatgpt']
        self.assertIn('이전 값 · 재로그인 필요', card.rows.itemcget('service_status','text'))
        self.assertIn('Codex CLI에 로그인해 주세요.',
                      [card.rows.itemcget(item,'text') for item in card.rows.find_withtag('error_text')
                       if card.rows.type(item)=='text'])
        self.assertTrue(card._actions)


def _contrast(foreground, background):
    def channel(value):
        value = value / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(color):
        red, green, blue = u._hex_rgb(color)
        return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)

    light, dark = max(luminance(foreground), luminance(background)), min(luminance(foreground), luminance(background))
    return (light + 0.05) / (dark + 0.05)


if __name__=='__main__':unittest.main(verbosity=2)
