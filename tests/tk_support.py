"""Helpers for tests that own a standalone Tk root."""


def destroy_root(root):
    # Tk queues a native idle theme notification during initialization. It is
    # not listed by `after info`; dispatch it while the window still exists.
    # Otherwise a later test can receive it after this root has been destroyed,
    # especially when a widget/image keeps its Tcl interpreter alive.
    try:
        root.update_idletasks()
    finally:
        root.destroy()
