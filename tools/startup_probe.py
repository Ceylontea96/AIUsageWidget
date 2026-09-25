"""Executed as usage_widget.py in benchmark_startup's disposable package."""
import time

entered = time.perf_counter()
import cProfile
import json
import os
from pathlib import Path
import sys

output = Path(os.environ['AIUSAGE_BENCH_DIR'])
(output / 'python.pid').write_text(str(os.getpid()))
events = {'python_entry': entered}
spans = []
profile = cProfile.Profile() if os.environ.get('AIUSAGE_BENCH_PROFILE') else None
if profile:
    profile.enable()

try:
    events['import_start'] = time.perf_counter()
    import widget_app as u
    events['import_end'] = time.perf_counter()

    # The real constructor, layout, assets, fonts, cached data and paint paths
    # run. Account polling/activity and Claude integration are excluded.
    import claude_integration
    claude_integration.ensure_bridge_copy = lambda: None
    u.UsageWidget.tick = lambda self: None
    u.UsageWidget._activity_tick = lambda self: None

    def timed(owner, name):
        original = getattr(owner, name)
        def call(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                spans.append({'name': name, 'ms': (time.perf_counter() - start) * 1000})
        setattr(owner, name, call)

    init = u.tk.Tk.__init__
    def transparent(root, *args, **kwargs):
        start = time.perf_counter()
        init(root, *args, **kwargs)
        root.attributes('-alpha', 0.0)
        spans.append({'name': 'Tk.__init__', 'ms': (time.perf_counter() - start) * 1000})
    u.tk.Tk.__init__ = transparent
    for name in ('build', '_load_icons', 'load_cache', 'apply_mode', 'relayout', 'place'):
        timed(u.UsageWidget, name)
    widget_init = u.UsageWidget.__init__
    holder = {}
    def construct(app, *args, **kwargs):
        events['constructor_start'] = time.perf_counter()
        widget_init(app, *args, **kwargs)
        events['constructor_end'] = time.perf_counter()
        holder['app'] = app
    u.UsageWidget.__init__ = construct

    def finish(root, *args, **kwargs):
        app = holder['app']
        root.update_idletasks()
        assert root.winfo_viewable(), 'widget was never mapped'
        events['ready'] = time.perf_counter()
        assert len(app.snapshots) == int(os.environ['AIUSAGE_BENCH_EXPECT_CACHE']), 'cache scenario was not restored'
        if profile:
            profile.disable()
            profile.dump_stats(str(output / 'startup.prof'))
        result = {'events': events, 'spans': spans, 'executable': sys.executable,
                  'python': sys.version, 'tk': root.tk.call('info', 'patchlevel'),
                  'cached_keys': sorted(app.snapshots)}
        (output / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        app.close()
    u.tk.Tk.mainloop = finish
    # Preserve production DPI setup, instance locking and window registration.
    u.main()
except BaseException:
    import traceback
    (output / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    raise
